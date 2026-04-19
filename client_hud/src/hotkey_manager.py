import time
import logging
from queue import Queue
from pynput import keyboard

logger = logging.getLogger("hotkey_manager")

class HotkeyAction:
    START_RECORDING = "START_RECORDING"
    STOP_RECORDING = "STOP_RECORDING"
    CANCEL = "CANCEL"

class HotkeyManager:
    """
    Manages global macOS keystrokes using pynput.
    Injects lightweight actions into a thread-safe queue to avoid blocking
    the macOS accessibility daemon.
    """
    def __init__(self, action_queue: Queue):
        self.action_queue = action_queue
        self.held_keys = set()
        
        # State tracking for Toggle Mode
        self.is_listening = False
        self.hotkey_handled = False
        
        # We listen for Cmd + Shift + J (Usually 'cmd', 'shift', 'j')
        self.target_hotkey = {
            keyboard.Key.cmd, 
            keyboard.Key.shift, 
            keyboard.KeyCode.from_char('j')
        }
        
    def _is_hotkey_pressed(self):
        return self.target_hotkey.issubset(self.held_keys)

    def on_press(self, key):
        self.held_keys.add(key)
        
        # Check for Escape/Cancel
        if key == keyboard.Key.esc:
            self.action_queue.put(HotkeyAction.CANCEL)
            return

        # Check for Hotkey match
        if self._is_hotkey_pressed():
            if not self.hotkey_handled:
                # Instantly mark as handled so OS key-repeat doesn't trigger 100 times intrinsically
                self.hotkey_handled = True
                
                if not self.is_listening:
                    self.is_listening = True
                    logger.info("Hotkey pressed: Toggling RECORD ON...")
                    self.action_queue.put(HotkeyAction.START_RECORDING)
                else:
                    self.is_listening = False
                    logger.info("Hotkey pressed: Toggling RECORD OFF...")
                    self.action_queue.put(HotkeyAction.STOP_RECORDING)

    def on_release(self, key):
        if key in self.held_keys:
            self.held_keys.remove(key)
            
        # If the hotkey was previously matched and we just released one of the modifiers/keys
        if not self._is_hotkey_pressed():
            self.hotkey_handled = False

    def start_listener(self):
        """Starts the listener daemon on a background thread."""
        logger.info("Initializing global hotkey listener...")
        self.listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release
        )
        self.listener.start()
        
    def stop_listener(self):
        if hasattr(self, 'listener'):
            self.listener.stop()
