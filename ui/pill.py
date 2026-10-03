"""Temporary native visual: a small always-on-top pill with the state, a level bar and a caption. No HTML.

Implements ui.frontend.VisualFrontend. All five methods are safe to call from any thread: they emit Qt signals, which
are delivered on the GUI thread. Click it to start listening; drag it to move it. It will be replaced by the final design,
and nothing outside this file depends on how it looks.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ui.frontend import STATES, clamp01

ACCENT = {"idle": "#8a8580", "listening": "#9a96f0", "thinking": "#f2a65a", "speaking": "#e26042"}
LABEL = {"idle": "Vex", "listening": "Listening…", "thinking": "Thinking…", "speaking": "Speaking"}
WIDTH, HEIGHT = 380, 92
DRAG_THRESHOLD = 5


class QtPillFrontend(QWidget):
    _state_sig = Signal(str)
    _level_sig = Signal(float)
    _caption_sig = Signal(str)
    _visible_sig = Signal(bool)
    _clear_caption_sig = Signal()

    def __init__(self, on_click=None, caption_seconds: float = 8.0, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(WIDTH, HEIGHT)
        self.on_click = on_click
        self.caption_seconds = caption_seconds
        self.state = "idle"
        self.level = 0.0                  # smoothed level shown
        self._target = 0.0                # latest level pushed by Python
        self.caption = ""
        self._press_pos: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._moved = False
        self._last_tick = time.monotonic()

        self._state_sig.connect(self._apply_state)
        self._level_sig.connect(self._apply_level)
        self._caption_sig.connect(self._apply_caption)
        self._visible_sig.connect(self._apply_visible)
        self._clear_caption_sig.connect(lambda: self._apply_caption(""))

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(33)
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(lambda: self._apply_caption(""))
        self._place_bottom_right()

    # ---- VisualFrontend (any thread) -----------------------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state in STATES:
            self._state_sig.emit(state)

    def set_level(self, level: float) -> None:
        self._level_sig.emit(clamp01(level))

    def show_caption(self, text: str) -> None:
        self._caption_sig.emit(str(text))

    def show(self) -> None:                      # noqa: D401 - overrides QWidget.show but is also thread-safe
        self._visible_sig.emit(True)

    def hide(self) -> None:
        self._visible_sig.emit(False)

    # ---- GUI-thread slots ----------------------------------------------------------------------------------------------
    def _apply_state(self, state: str) -> None:
        self.state = state
        self._target = 0.0
        if state == "idle" and self.caption:
            self._clear_timer.start(int(self.caption_seconds * 1000))
        elif state != "idle":
            self._clear_timer.stop()
        self.update()

    def _apply_level(self, level: float) -> None:
        self._target = level

    def _apply_caption(self, text: str) -> None:
        self.caption = text
        self.setToolTip(text)
        if text and self.state == "idle":
            self._clear_timer.start(int(self.caption_seconds * 1000))
        self.update()

    def _apply_visible(self, visible: bool) -> None:
        QWidget.setVisible(self, visible)

    def _place_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.right() - WIDTH - 24, g.bottom() - HEIGHT - 24)

    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        rate = 20 if self._target > self.level else 5           # fast attack, slow release, like the orb
        self.level += (self._target - self.level) * min(1.0, dt * rate)
        if self.isVisible():
            self.update()

    # ---- painting ------------------------------------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = QColor(ACCENT.get(self.state, ACCENT["idle"]))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(22, 20, 19, 225))
        p.drawRoundedRect(QRectF(0.5, 0.5, WIDTH - 1, HEIGHT - 1), 20, 20)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 90), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, WIDTH - 1, HEIGHT - 1), 20, 20)

        # state dot (pulses while listening/thinking, swells with the voice while speaking)
        t = time.monotonic()
        pulse = {"listening": 0.5 + 0.5 * math.sin(t * 5), "thinking": 0.5 + 0.5 * math.sin(t * 9),
                 "speaking": self.level}.get(self.state, 0.0)
        radius = 7 + 3 * pulse
        p.setPen(Qt.NoPen)
        glow = QColor(accent)
        glow.setAlpha(int(40 + 90 * pulse))
        p.setBrush(glow)
        p.drawEllipse(QRectF(26 - radius * 1.8, 26 - radius * 1.8, radius * 3.6, radius * 3.6))
        p.setBrush(accent)
        p.drawEllipse(QRectF(26 - radius, 26 - radius, radius * 2, radius * 2))

        p.setPen(QColor(235, 230, 225))
        f = QFont(self.font())
        f.setPointSizeF(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(48, 12, 150, 28), Qt.AlignVCenter | Qt.AlignLeft, LABEL.get(self.state, "Vex"))

        # level bar
        bar = QRectF(WIDTH - 130, 20, 106, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(bar, 6, 6)
        if self.level > 0.01:
            fill = QRectF(bar.left(), bar.top(), max(12.0, bar.width() * self.level), bar.height())
            p.setBrush(accent)
            p.drawRoundedRect(fill, 6, 6)

        # caption (two lines, clipped)
        if self.caption:
            f.setBold(False)
            f.setPointSizeF(9.5)
            p.setFont(f)
            p.setPen(QColor(200, 195, 190))
            p.drawText(QRectF(18, 46, WIDTH - 36, HEIGHT - 52), Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft, self.caption)
        p.end()

    # ---- mouse: click to listen, drag to move ----------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
            self._moved = False

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._press_pos is not None and self._drag_offset is not None:
            pos = e.globalPosition().toPoint()
            if (pos - self._press_pos).manhattanLength() > DRAG_THRESHOLD:
                self._moved = True
            if self._moved:
                self.move(pos - self._drag_offset)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self._press_pos is not None:
            if not self._moved and self.on_click is not None:
                self.on_click("click")
            self._press_pos = self._drag_offset = None
