import sys
import logging
from queue import Queue, Empty
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QPalette, QFont

logger = logging.getLogger("orion_hud")

class OrionHUD(QWidget):
    # Signals for thread-safe cross-thread UI updates
    update_text_signal = pyqtSignal(str)
    update_state_signal = pyqtSignal(str)

    def __init__(self, action_queue: Queue, display_queue: Queue):
        super().__init__()
        self.action_queue = action_queue
        self.display_queue = display_queue
        self.current_state = "IDLE"

        self._init_ui()
        self._start_polling_timer()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # Position at bottom center of screen
        screen = QApplication.primaryScreen().geometry()
        width = 600
        height = 100
        x = (screen.width() - width) // 2
        y = screen.height() - height - 100
        self.setGeometry(x, y, width, height)

        # Main Layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Capsule Label
        self.capsule = QLabel("Orion System initialized...")
        self.capsule.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        font = QFont(".SF Pro Display", 18, QFont.Weight.Medium)
        self.capsule.setFont(font)
        
        self.capsule.setStyleSheet(self._get_stylesheet("rgba(40, 40, 40, 0.8)", "#FFFFFF"))
        
        layout.addWidget(self.capsule)
        self.setLayout(layout)

        # Wire up signals
        self.update_text_signal.connect(self._set_text)
        self.update_state_signal.connect(self._set_state)

        # Hide initially
        self.hide()

    def _get_stylesheet(self, bg_color: str, text_color: str) -> str:
        return f"""
            QLabel {{
                background-color: {bg_color};
                color: {text_color};
                border-radius: 25px;
                padding: 15px 30px;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }}
        """

    def _set_text(self, text: str):
        self.capsule.setText(text)

    def _set_state(self, state: str):
        self.current_state = state
        if state == "RECORDING":
            self.show()
            self.raise_()
            self.capsule.setStyleSheet(self._get_stylesheet("rgba(200, 40, 40, 0.9)", "#FFFFFF")) # Red
        elif state == "REASONING":
            self.show()
            self.raise_()
            self.capsule.setStyleSheet(self._get_stylesheet("rgba(200, 150, 40, 0.95)", "#000000")) # Yellow
        elif state == "SPEAKING":
            self.show()
            self.raise_()
            self.capsule.setStyleSheet(self._get_stylesheet("rgba(40, 180, 80, 0.9)", "#FFFFFF")) # Green
        elif state == "ERROR":
            self.show()
            self.capsule.setStyleSheet(self._get_stylesheet("rgba(255, 0, 0, 1.0)", "#FFFFFF")) # Sharp Red
        elif state == "IDLE":
            self.hide()

    def _start_polling_timer(self):
        """Poll the queues continuously safely on main thread."""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_queues)
        self.timer.start(50)  # 20 FPS polling limit

    def _poll_queues(self):
        while not self.display_queue.empty():
            try:
                event = self.display_queue.get_nowait()
                if "state" in event:
                    self.update_state_signal.emit(event["state"])
                if "text" in event:
                    self.update_text_signal.emit(event["text"])
            except Empty:
                break

def launch_hud(action_queue: Queue, display_queue: Queue, app=None):
    import sys
    if not app:
        app = QApplication(sys.argv)
    hud = OrionHUD(action_queue, display_queue)
    sys.exit(app.exec())
