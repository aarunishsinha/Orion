import os
import sys
import json
import time
import logging
import threading
import requests
from queue import Queue
import redis

# Bridge the path to allow importing from the isolated client_hud UI module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../client_hud')))

# Internal Modules
from src.hotkey_manager import HotkeyManager, HotkeyAction
from src.audio_engine import AudioEngine
from src.tts_engine import TTSEngine
from src.orion_hud import launch_hud

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Ollama / Qwen2.5-3B Configuration
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "60"))

SYSTEM_PROMPT = (
    "You are Orion, a helpful and concise voice assistant running natively on macOS. "
    "You receive transcribed speech from the user's microphone. "
    "Respond in 1-3 short sentences — your replies will be displayed on a small overlay HUD "
    "and eventually spoken aloud via TTS, so brevity is critical. "
    "Be warm, direct, and avoid unnecessary filler."
)

# Rolling conversation window — keeps the last N exchanges for multi-turn context.
MAX_HISTORY_TURNS = 20
conversation_history: list[dict] = []


def _trim_history():
    """Keep conversation_history bounded to MAX_HISTORY_TURNS messages."""
    global conversation_history
    if len(conversation_history) > MAX_HISTORY_TURNS:
        conversation_history = conversation_history[-MAX_HISTORY_TURNS:]


def _check_ollama_health() -> bool:
    """Verify Ollama is reachable before attempting inference."""
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        return resp.status_code == 200
    except requests.ConnectionError:
        return False


def query_llm(user_text: str) -> tuple[str, dict]:
    """
    Send the user's transcript to Qwen2.5-3B via Ollama and return the
    assistant's response plus Ollama's timing stats (empty on failure).
    Maintains rolling conversation history for multi-turn context.
    """
    conversation_history.append({"role": "user", "content": user_text})
    _trim_history()

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *conversation_history,
        ],
        "stream": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 256,  # Cap output tokens for voice-assistant brevity
        },
    }

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()

        result = resp.json()
        assistant_msg = result["message"]["content"].strip()

        conversation_history.append({"role": "assistant", "content": assistant_msg})
        _trim_history()

        # Ollama reports durations in nanoseconds
        stats = {k: result.get(k) for k in (
            "total_duration", "load_duration",
            "prompt_eval_count", "prompt_eval_duration",
            "eval_count", "eval_duration",
        )}

        logger.info(f"LLM response ({len(assistant_msg)} chars): {assistant_msg[:120]}...")
        return assistant_msg, stats

    except requests.ConnectionError:
        logger.error("Ollama is not running — cannot reach %s", OLLAMA_BASE_URL)
        return "I can't reach my language model right now. Is Ollama running?", {}
    except requests.Timeout:
        logger.error("Ollama request timed out after %ds", OLLAMA_TIMEOUT)
        return "Sorry, I took too long to think. Please try again.", {}
    except requests.HTTPError as e:
        logger.error("Ollama HTTP error: %s", e)
        return "Something went wrong with the language model.", {}
    except (KeyError, ValueError) as e:
        logger.error("Failed to parse Ollama response: %s", e)
        return "I received a garbled response. Please try again.", {}


# ---------------------------------------------------------------------------
# Latency benchmark (ORION_BENCH=1)
# ---------------------------------------------------------------------------
# When enabled, conversation history is cleared before every turn so runs are
# comparable, and per-stage timings are appended to logs/latency.jsonl.
BENCH_MODE = os.environ.get("ORION_BENCH") == "1"
LATENCY_LOG = os.path.abspath(os.path.join(os.path.dirname(__file__), "../logs/latency.jsonl"))
_bench_turn = 0


def _record_latency(record: dict):
    global _bench_turn
    _bench_turn += 1
    record = {"ts": time.time(), "pid": os.getpid(), "turn": _bench_turn, **record}
    os.makedirs(os.path.dirname(LATENCY_LOG), exist_ok=True)
    with open(LATENCY_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    logger.info("Latency: stt=%.3fs llm=%.3fs tts=%.3fs total=%.3fs",
                record["stt_s"], record["llm_s"], record["tts_s"], record["total_s"])



# ---------------------------------------------------------------------------
# Redis Connection
# ---------------------------------------------------------------------------
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6380"))  # docker-compose maps Redis to host 6380

try:
    redis_client = redis.Redis(host='localhost', port=REDIS_PORT, decode_responses=True)
    redis_client.ping() # Force the lazy socket to evaluate
except Exception as e:
    logger.error(f"Failed to connect to Redis (Running in offline mode): {e}")
    redis_client = None


# ---------------------------------------------------------------------------
# Orchestrator Daemon
# ---------------------------------------------------------------------------
def orchestrator_daemon(action_queue: Queue, display_queue: Queue,
                        audio_engine: AudioEngine, tts_engine: TTSEngine):
    """
    Background daemon that watches the Hotkey actions and manages the LLM workflows
    without blocking the primary PyQt6 visual thread.
    """
    logger.info("Orchestrator daemon online and waiting for hotkey events...")

    # Pre-flight: verify Ollama is reachable at startup
    if _check_ollama_health():
        logger.info("Ollama is reachable at %s (model: %s)", OLLAMA_BASE_URL, OLLAMA_MODEL)
    else:
        logger.warning(
            "Ollama is NOT reachable at %s — LLM calls will fail until it is started. "
            "Run: ollama serve && ollama pull %s",
            OLLAMA_BASE_URL, OLLAMA_MODEL,
        )

    while True:
        action = action_queue.get()

        if action == HotkeyAction.START_RECORDING:
            # 1. Lock Check Rule
            if redis_client and redis_client.get("orion:status:busy") == "true":
                logger.warning("Orion is busy. Rejecting prompt.")
                display_queue.put({"state": "ERROR", "text": "Orion is busy."})
                time.sleep(1)
                display_queue.put({"state": "IDLE"})
                continue
            
            # Lock the system
            if redis_client:
                redis_client.set("orion:status:busy", "true")
            
            display_queue.put({"state": "RECORDING", "text": "Listening..."})
            audio_engine.start_recording()

        elif action == HotkeyAction.STOP_RECORDING:
            # Show transcribing state while whisper-cli processes
            display_queue.put({"state": "REASONING", "text": "Transcribing..."})
            t_stt = time.perf_counter()
            text = audio_engine.stop_recording_and_transcribe()
            stt_s = time.perf_counter() - t_stt

            if not text:
                display_queue.put({"state": "IDLE"})
                if redis_client:
                    redis_client.set("orion:status:busy", "false")
                continue
                
            # Show transcript so user can read it
            logger.info(f"Dispatching to LLM: '{text}'")
            display_queue.put({"state": "REASONING", "text": f"Orion heard: '{text}'"})

            # Real LLM call via Ollama (transcript stays visible while it runs)
            if BENCH_MODE:
                conversation_history.clear()
            t_llm = time.perf_counter()
            response, llm_stats = query_llm(text)
            llm_s = time.perf_counter() - t_llm

            # TTS: generate audio, measure duration, sync typing to speech
            try:
                t_tts = time.perf_counter()
                duration, playback = tts_engine.speak(response)  # returns once playback has started
                tts_s = time.perf_counter() - t_tts
                if BENCH_MODE:
                    _record_latency({
                        "audio_s": audio_engine.last_audio_seconds,
                        "stt_s": stt_s, "llm_s": llm_s, "tts_s": tts_s,
                        "total_s": time.perf_counter() - t_stt,
                        "transcript": text, "response_chars": len(response),
                        "llm_ok": bool(llm_stats), **llm_stats,
                    })
                char_delay = (duration / max(len(response), 1)) * 1000  # ms per char
                display_queue.put({
                    "state": "SPEAKING",
                    "text": response,
                    "char_delay": char_delay,
                })
                playback.wait()   # block until speech finishes
                time.sleep(1.5)   # brief post-speech pause
            except Exception as e:
                logger.error("TTS failed, falling back to silent display: %s", e)
                display_queue.put({"state": "SPEAKING", "text": response})
                time.sleep(max(3.0, min(len(response) * 0.040 + 2.0, 20.0)))
            
            # End Task
            if redis_client:
                redis_client.set("orion:status:busy", "false")
            display_queue.put({"state": "IDLE"})

        elif action == HotkeyAction.CANCEL:
            logger.error("Global Cancel Initiated!")
            audio_engine.stop_recording_and_transcribe() # Flush
            tts_engine.cancel()                          # Stop any speech
            if redis_client:
                # Panic switch: clear only Orion's keys — Redis is shared with the calendar stack
                for key in redis_client.scan_iter(match="orion:*"):
                    redis_client.delete(key)
                redis_client.set("orion:status:busy", "false")
            display_queue.put({"state": "IDLE"})

def main():
    # 0. MUST BE DONE FIRST ON MACOS! Prevent generic Context crash.
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)

    # 1. Setup inter-thread Queues safely
    action_queue = Queue()
    display_queue = Queue()

    # 2. Instantiate Components
    audio_engine = AudioEngine(display_queue)
    tts_engine = TTSEngine()
    hotkey_manager = HotkeyManager(action_queue)

    # 3. Start Hotkey Daemon
    hotkey_manager.start_listener()

    # 4. Start Orchestrator Background Brain
    orch_thread = threading.Thread(
        target=orchestrator_daemon, 
        args=(action_queue, display_queue, audio_engine, tts_engine),
        daemon=True
    )
    orch_thread.start()

    # 5. Launch PyQt6 UI on Main Thread (Blocking)
    logger.info("Initializing PyQt6 Main Display...")
    # NOTE: It is imperative this remains on the absolute lowest caller level
    # due to MacOS Native Window manager policies.
    launch_hud(action_queue, display_queue, app)
    
    # 6. Cleanup on GUI Exit
    hotkey_manager.stop_listener()
    tts_engine.cancel()
    audio_engine.kill_all()

if __name__ == "__main__":
    main()
