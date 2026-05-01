import sys
import logging
from queue import Queue, Empty
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QGraphicsDropShadowEffect
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve, QRect, QPoint
from PyQt6.QtGui import QColor, QPalette, QFont, QPainter, QPainterPath, QBrush, QPen, QFontMetrics

logger = logging.getLogger("orion_hud")

# ---------------------------------------------------------------------------
# Design Tokens — muted, modern palette
# ---------------------------------------------------------------------------
_FILL_ALPHA = 179  # 70% opaque

THEME = {
    "RECORDING": {
        "border": QColor(140, 30, 30, 255),          # dark crimson border
        "fill":   QColor(90, 20, 20, _FILL_ALPHA),   # dark red fill — 50% opaque
        "text":   QColor(255, 235, 235),
        "dot":    QColor(255, 100, 100),
    },
    "REASONING": {
        "border": QColor(140, 110, 30, 255),          # dark amber border
        "fill":   QColor(80, 60, 10, _FILL_ALPHA),    # dark amber fill
        "text":   QColor(255, 248, 230),
        "dot":    QColor(230, 200, 100),
    },
    "SPEAKING": {
        "border": QColor(30, 120, 80, 255),           # dark teal border
        "fill":   QColor(15, 70, 45, _FILL_ALPHA),    # dark teal fill
        "text":   QColor(230, 255, 245),
        "dot":    QColor(100, 210, 160),
    },
    "ERROR": {
        "border": QColor(160, 30, 30, 255),           # deep red border
        "fill":   QColor(100, 15, 15, _FILL_ALPHA),   # dark red fill
        "text":   QColor(255, 220, 220),
        "dot":    QColor(255, 80, 80),
    },
}

# Layout constants
HUD_WIDTH = 360
HUD_MARGIN_TOP = 40
HUD_MARGIN_RIGHT = 24
HUD_PADDING_H = 20
HUD_PADDING_V = 16
BORDER_RADIUS = 14
BORDER_WIDTH = 2
MIN_HEIGHT = 60
MAX_HEIGHT = 400

# Typing animation
TYPING_CHAR_DELAY_MS = 40   # milliseconds per character
TYPING_CURSOR = "▌"         # cursor glyph


class OrionHUD(QWidget):
    """
    A translucent overlay panel anchored to the top-right corner of the screen.
    Renders a rounded-rect card with an opaque border and a highly translucent
    fill. The card expands downward as text wraps. LLM responses are animated
    with a character-by-character typing effect.
    """

    # Signals for thread-safe cross-thread UI updates
    update_text_signal = pyqtSignal(str, float)  # (text, char_delay_ms)
    update_state_signal = pyqtSignal(str)

    def __init__(self, action_queue: Queue, display_queue: Queue):
        super().__init__()
        self.action_queue = action_queue
        self.display_queue = display_queue
        self.current_state = "IDLE"
        self._current_theme = THEME["RECORDING"]

        # Typing animation state
        self._typing_full_text = ""
        self._typing_pos = 0
        self._typing_active = False

        self._init_ui()
        self._start_polling_timer()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Main label — word-wrapped
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.label.setContentsMargins(HUD_PADDING_H, HUD_PADDING_V, HUD_PADDING_H, HUD_PADDING_V)

        font = QFont("Menlo", 13, QFont.Weight.Normal)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.label.setFont(font)
        self.label.setStyleSheet("background: transparent; border: none;")

        # Status dot label
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setContentsMargins(HUD_PADDING_H, 6, HUD_PADDING_H, 0)
        status_font = QFont("Menlo", 10, QFont.Weight.DemiBold)
        self.status_label.setFont(status_font)
        self.status_label.setStyleSheet("background: transparent; border: none;")

        # Layout
        layout = QVBoxLayout()
        layout.setContentsMargins(BORDER_WIDTH, BORDER_WIDTH, BORDER_WIDTH, BORDER_WIDTH)
        layout.setSpacing(0)
        layout.addWidget(self.status_label)
        layout.addWidget(self.label)
        self.setLayout(layout)

        # Wire up signals
        self.update_text_signal.connect(self._on_text_event)
        self.update_state_signal.connect(self._set_state)

        # Typing animation timer (created once, started/stopped as needed)
        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._typing_tick)

        # Hide initially
        self.hide()

    # ------------------------------------------------------------------
    # Custom paint — rounded rect with opaque border + translucent fill
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        theme = self._current_theme
        rect = self.rect().adjusted(1, 1, -1, -1)

        # Draw filled rounded rect (highly translucent)
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()),
                            BORDER_RADIUS, BORDER_RADIUS)
        painter.fillPath(path, QBrush(theme["fill"]))

        # Draw opaque border
        pen = QPen(theme["border"], BORDER_WIDTH)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, BORDER_RADIUS, BORDER_RADIUS)

        painter.end()

    # ------------------------------------------------------------------
    # Sizing & positioning
    # ------------------------------------------------------------------
    def _reposition(self):
        """Anchor the widget to the top-right of the primary screen and
        resize height to fit content (expanding downward)."""
        screen = QApplication.primaryScreen().geometry()

        self.label.adjustSize()
        content_h = (self.status_label.sizeHint().height() +
                     self.label.heightForWidth(HUD_WIDTH - 2 * HUD_PADDING_H - 2 * BORDER_WIDTH) +
                     HUD_PADDING_V * 2 + 6 + BORDER_WIDTH * 2)
        height = max(MIN_HEIGHT, min(content_h, MAX_HEIGHT))

        x = screen.width() - HUD_WIDTH - HUD_MARGIN_RIGHT
        y = HUD_MARGIN_TOP
        self.setGeometry(x, y, HUD_WIDTH, height)

    # ------------------------------------------------------------------
    # Typing animation
    # ------------------------------------------------------------------
    def _start_typing(self, full_text: str, char_delay_ms: float = 0):
        """Begin character-by-character reveal of the given text.
        If char_delay_ms > 0, use it instead of the default TYPING_CHAR_DELAY_MS."""
        self._stop_typing()
        self._typing_full_text = full_text
        self._typing_pos = 0
        self._typing_active = True
        delay = int(char_delay_ms) if char_delay_ms > 0 else TYPING_CHAR_DELAY_MS
        self._typing_timer.start(delay)

    def _typing_tick(self):
        """Advance the typing cursor by one character."""
        if self._typing_pos >= len(self._typing_full_text):
            # Animation complete — show final text without cursor
            self._stop_typing()
            self._set_label_text(self._typing_full_text)
            return

        self._typing_pos += 1
        visible = self._typing_full_text[:self._typing_pos]
        self._set_label_text(visible + TYPING_CURSOR)

    def _stop_typing(self):
        """Cancel any in-progress typing animation."""
        self._typing_timer.stop()
        self._typing_active = False

    def _set_label_text(self, text: str):
        """Set the label text and reposition the card to fit."""
        color = self._current_theme["text"].name()
        self.label.setText(text)
        self.label.setStyleSheet(f"background: transparent; border: none; color: {color};")
        self._reposition()

    # ------------------------------------------------------------------
    # State & text updates
    # ------------------------------------------------------------------
    def _on_text_event(self, text: str, char_delay_ms: float = 0):
        """Handle incoming text — use typing animation for SPEAKING state,
        instant display for everything else."""
        if self.current_state == "SPEAKING":
            self._start_typing(text, char_delay_ms)
        else:
            self._stop_typing()
            self._set_label_text(text)

    def _set_state(self, state: str):
        self.current_state = state

        if state == "IDLE":
            self._stop_typing()
            self.hide()
            return

        theme = THEME.get(state, THEME["RECORDING"])
        self._current_theme = theme

        # If switching away from SPEAKING, kill any running animation
        if state != "SPEAKING":
            self._stop_typing()

        # Status indicator text
        state_labels = {
            "RECORDING":  "● RECORDING",
            "REASONING":  "◐ THINKING",
            "SPEAKING":   "◉ RESPONSE",
            "ERROR":      "✕ ERROR",
        }
        dot_color = theme["dot"].name()
        text_color = theme["text"].name()
        self.status_label.setText(state_labels.get(state, state))
        self.status_label.setStyleSheet(
            f"background: transparent; border: none; "
            f"color: {dot_color}; letter-spacing: 1px;"
        )

        self.label.setStyleSheet(f"background: transparent; border: none; color: {text_color};")

        self._reposition()
        self.show()
        self.raise_()
        self.update()

    # ------------------------------------------------------------------
    # Queue polling
    # ------------------------------------------------------------------
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
                    char_delay = float(event.get("char_delay", 0))
                    self.update_text_signal.emit(event["text"], char_delay)
            except Empty:
                break


def launch_hud(action_queue: Queue, display_queue: Queue, app=None):
    import sys
    if not app:
        app = QApplication(sys.argv)
    hud = OrionHUD(action_queue, display_queue)
    sys.exit(app.exec())
