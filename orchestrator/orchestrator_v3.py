import os
import sys
import time
import logging
import threading
from queue import Queue
import redis

# Bridge the path to allow importing from the isolated client_hud UI module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../client_hud')))

# Internal Modules
from src.hotkey_manager import HotkeyManager, HotkeyAction
from src.audio_engine import AudioEngine
from src.orion_hud import launch_hud

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("orchestrator")

try:
    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    redis_client.ping() # Force the lazy socket to evaluate
except Exception as e:
    logger.error(f"Failed to connect to Redis (Running in offline mode): {e}")
    redis_client = None

def orchestrator_daemon(action_queue: Queue, display_queue: Queue, audio_engine: AudioEngine):
    """
    Background daemon that watches the Hotkey actions and manages the LLM workflows
    without blocking the primary PyQt6 visual thread.
    """
    logger.info("Orchestrator daemon online and waiting for hotkey events...")

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
            time.sleep(3)
            
            # Simulate LLM processing
            display_queue.put({"state": "REASONING", "text": "Thinking..."})
            time.sleep(2)
            
            # Simulate TTS / response
            display_queue.put({"state": "SPEAKING", "text": "Responding..."})
            time.sleep(3)
            
            # End Task
            if redis_client:
                redis_client.set("orion:status:busy", "false")
            display_queue.put({"state": "IDLE"})

        elif action == HotkeyAction.CANCEL:
            logger.error("Global Cancel Initiated!")
            audio_engine.stop_recording_and_transcribe() # Flush
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
    hotkey_manager = HotkeyManager(action_queue)

    # 3. Start Hotkey Daemon
    hotkey_manager.start_listener()

    # 4. Start Orchestrator Background Brain
    orch_thread = threading.Thread(
        target=orchestrator_daemon, 
        args=(action_queue, display_queue, audio_engine),
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
    audio_engine.kill_all()

if __name__ == "__main__":
    main()
