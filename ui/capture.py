"""Typed stand-in for the microphone until Phase 2 (recorder + speech-to-text).

When Vex starts "listening", a small prompt appears; Enter sends what you typed, Esc cancels. It implements the same
Capture interface the controller uses for the real microphone capture later.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QVBoxLayout


class _PromptDialog(QDialog):
    def __init__(self, timeout_s: int):
        super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.text: str | None = None
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        hint = QLabel("Listening: type what you would say (Enter to send, Esc to cancel)")
        hint.setTextFormat(Qt.PlainText)
        self.edit = QLineEdit()
        self.edit.returnPressed.connect(self._submit)
        lay.addWidget(hint)
        lay.addWidget(self.edit)
        self.setStyleSheet("QDialog{background:#161413;border:1px solid #9a96f0;border-radius:10px;}"
                           "QLabel{color:#c8c3be;}QLineEdit{padding:6px;font-size:14px;}")
        QTimer.singleShot(timeout_s * 1000, self.reject)

    def _submit(self) -> None:
        self.text = self.edit.text()
        self.accept()

    def present(self) -> None:
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.center().x() - self.width() // 2, g.bottom() - 220)
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()


class _Wait:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.text: str | None = None


class TextPromptCapture(QObject):
    """Callable Capture: returns the typed text, or None if cancelled or timed out. Create on the GUI thread."""

    _requested = Signal(object)
    _cancel_requested = Signal()

    def __init__(self, timeout_s: int = 30, parent=None):
        super().__init__(parent)
        self.timeout_s = timeout_s
        self.active_dialog: _PromptDialog | None = None
        self._requested.connect(self._show)
        self._cancel_requested.connect(self._cancel)

    def __call__(self) -> str | None:
        if QThread.currentThread() is self.thread():
            dlg = _PromptDialog(self.timeout_s)
            dlg.present()
            dlg.exec()
            return dlg.text
        wait = _Wait()
        self._requested.emit(wait)
        if not wait.done.wait(self.timeout_s + 5):
            self._cancel_requested.emit()
            return None
        return wait.text

    def cancel(self) -> None:
        self._cancel_requested.emit()

    @Slot(object)
    def _show(self, wait: _Wait) -> None:
        dlg = _PromptDialog(self.timeout_s)
        self.active_dialog = dlg

        def finished(_code: int = 0) -> None:
            wait.text = dlg.text
            if self.active_dialog is dlg:
                self.active_dialog = None
            wait.done.set()
            dlg.deleteLater()

        dlg.finished.connect(finished)
        dlg.present()

    @Slot()
    def _cancel(self) -> None:
        if self.active_dialog is not None:
            self.active_dialog.reject()
