import os
import sys
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


def query_llm(user_text: str) -> str:
    """
    Send the user's transcript to Qwen2.5-3B via Ollama and return the
    assistant's response. Maintains rolling conversation history for
    multi-turn context.
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

        logger.info(f"LLM response ({len(assistant_msg)} chars): {assistant_msg[:120]}...")
        return assistant_msg

    except requests.ConnectionError:
        logger.error("Ollama is not running — cannot reach %s", OLLAMA_BASE_URL)
        return "I can't reach my language model right now. Is Ollama running?"
    except requests.Timeout:
        logger.error("Ollama request timed out after %ds", OLLAMA_TIMEOUT)
        return "Sorry, I took too long to think. Please try again."
    except requests.HTTPError as e:
        logger.error("Ollama HTTP error: %s", e)
        return "Something went wrong with the language model."
    except (KeyError, ValueError) as e:
        logger.error("Failed to parse Ollama response: %s", e)
        return "I received a garbled response. Please try again."



# ---------------------------------------------------------------------------
# Redis Connection
# ---------------------------------------------------------------------------
try:
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
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
            text = audio_engine.stop_recording_and_transcribe()
            
            if not text:
                display_queue.put({"state": "IDLE"})
                if redis_client:
                    redis_client.set("orion:status:busy", "false")
                continue
                
            # Show transcript so user can read it
            logger.info(f"Dispatching to LLM: '{text}'")
            display_queue.put({"state": "REASONING", "text": f"Orion heard: '{text}'"})
            time.sleep(2)
            
            # Real LLM call via Ollama
            display_queue.put({"state": "REASONING", "text": "Thinking..."})
            response = query_llm(text)

            # TTS: generate audio, measure duration, sync typing to speech
            try:
                duration, playback = tts_engine.speak(response)
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
                redis_client.flushall() # Specific panic switch requirement
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
