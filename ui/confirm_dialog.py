"""Native Qt confirmation dialogs: the user-facing side of the Confirmer interface.

Rules the dialog enforces itself:
  * It shows the exact resolved command/path (req.detail) as plain text in a read-only monospace box. Every label is
    PlainText, so nothing a command or file name contains can be rendered as markup or hide part of the text.
  * Yellow: Enter approves, Esc denies.
  * Red: Enter does NOT approve (Deny is the default button), Esc denies; approving needs a click (or Tab to Approve and
    Space). For the typed CONFIRM step the Approve button stays disabled until the exact word is typed.
  * Buttons are disabled for a short moment after the dialog appears, so a keystroke meant for another window can't
    approve (or deny) anything by accident.
  * No answer within the request's timeout means no.
Approvals come back on the "dialog" channel, which is not voice, so red and typed requests accept it.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from safety.confirm import Confirmation, ConfirmRequest

_BADGE = {"red": "background:#b3261e;color:white;", "yellow": "background:#c77c00;color:white;"}
_WARN = {"red": "color:#b3261e;", "yellow": "color:#8a5a00;"}
_STEP_TEXT = {"action": "", "catastrophic": "Second confirmation: this command is destructive",
              "elevation": "Separate approval: administrator rights"}


def _plain(text: str, parent=None, wrap: bool = True) -> QLabel:
    label = QLabel(text, parent)
    label.setTextFormat(Qt.PlainText)                  # never interpret LLM-controlled text as rich text
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class ConfirmDialog(QDialog):
    def __init__(self, req: ConfirmRequest, input_guard_ms: int = 500, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.req = req
        self.result_value: Confirmation | None = None
        self._armed = input_guard_ms <= 0
        self._remaining = max(1, int(req.timeout))
        self._typed = req.kind == "typed"
        self.setWindowTitle("Vex needs your approval")
        self.setMinimumWidth(540)
        self.setModal(True)

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        self.badge = _plain(req.tier.upper(), self, wrap=False)
        self.badge.setStyleSheet(_BADGE.get(req.tier, _BADGE["red"]) + "padding:2px 8px;border-radius:4px;font-weight:bold;")
        self.title_label = _plain(req.title, self)
        self.title_label.setStyleSheet("font-weight:bold;font-size:14px;")
        head.addWidget(self.badge, 0, Qt.AlignTop)
        head.addWidget(self.title_label, 1)
        root.addLayout(head)
        if step := _STEP_TEXT.get(req.step):
            self.step_label = _plain(step, self)
            root.addWidget(self.step_label)

        self.detail_view = QPlainTextEdit(self)
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlainText(req.detail)
        self.detail_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.detail_view.setMinimumHeight(90)
        self.detail_view.setMaximumHeight(280)
        root.addWidget(self.detail_view)

        self.warning_labels: list[QLabel] = []
        for w in req.warnings:
            lab = _plain("⚠ " + w, self)
            lab.setStyleSheet(_WARN.get(req.tier, _WARN["red"]))
            self.warning_labels.append(lab)
            root.addWidget(lab)

        self.typed_edit: QLineEdit | None = None
        if self._typed:
            root.addWidget(_plain(f"Type {req.typed_word} to enable Approve:", self))
            self.typed_edit = QLineEdit(self)
            self.typed_edit.setPlaceholderText(req.typed_word or "")
            self.typed_edit.textChanged.connect(self._update_enabled)
            root.addWidget(self.typed_edit)

        self.countdown_label = _plain("", self)
        self.countdown_label.setStyleSheet("color:#777;")
        root.addWidget(self.countdown_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.deny_btn = QPushButton("Deny (Esc)", self)
        self.approve_btn = QPushButton("Approve", self)
        buttons.addWidget(self.deny_btn)
        buttons.addWidget(self.approve_btn)
        root.addLayout(buttons)
        self.deny_btn.clicked.connect(self._deny)
        self.approve_btn.clicked.connect(self._approve)

        # which button Enter triggers: yellow and typed -> Approve; plain red -> Deny
        approve_is_default = req.tier == "yellow" or self._typed
        self.approve_btn.setAutoDefault(approve_is_default)
        self.approve_btn.setDefault(approve_is_default)
        self.deny_btn.setAutoDefault(not approve_is_default)
        self.deny_btn.setDefault(not approve_is_default)
        (self.typed_edit or (self.approve_btn if approve_is_default else self.deny_btn)).setFocus()

        self._update_enabled()
        self._tick()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        if not self._armed:
            QTimer.singleShot(input_guard_ms, self._arm)

    # ---- state ---------------------------------------------------------------------------------------------------------
    def _arm(self) -> None:
        self._armed = True
        self._update_enabled()

    def _typed_ok(self) -> bool:
        return not self._typed or (self.typed_edit is not None and self.typed_edit.text() == self.req.typed_word)

    def _update_enabled(self) -> None:
        self.deny_btn.setEnabled(self._armed)
        self.approve_btn.setEnabled(self._armed and self._typed_ok())

    def _tick(self) -> None:
        self.countdown_label.setText(f"No answer in {self._remaining} s counts as No.")
        if self._remaining <= 0:
            self._finish(Confirmation(False, "none", "timeout"), accept=False)
            return
        self._remaining -= 1

    # ---- outcomes ------------------------------------------------------------------------------------------------------
    def _finish(self, result: Confirmation, accept: bool) -> None:
        if self.result_value is None:
            self.result_value = result
        self._timer.stop()
        self.accept() if accept else self.reject()

    def _approve(self) -> None:
        if self.approve_btn.isEnabled() and self._armed and self._typed_ok():
            self._finish(Confirmation(True, "dialog"), accept=True)

    def _deny(self) -> None:
        if self._armed:
            self._finish(Confirmation(False, "dialog", "declined"), accept=False)

    def reject(self) -> None:                       # Esc or the window's close button
        if self.result_value is None:
            self.result_value = Confirmation(False, "dialog", "declined")
        self._timer.stop()
        super().reject()

    def closeEvent(self, event) -> None:
        if self.result_value is None:
            self.result_value = Confirmation(False, "dialog", "declined")
        self._timer.stop()
        super().closeEvent(event)

    def present(self) -> None:
        """Show on top, centred, and flash the taskbar so it can't be missed."""
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self.show()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self, 0)


class _Pending:
    def __init__(self, req: ConfirmRequest):
        self.req = req
        self.done = threading.Event()
        self.result = Confirmation(False, "none", "no_input")


class QtConfirmer(QObject):
    """Confirmer backed by native dialogs. Create it on the GUI thread; confirm() may be called from any thread."""

    _show_requested = Signal(object)
    _close_requested = Signal()

    def __init__(self, input_guard_ms: int = 500, notify=None, parent=None):
        super().__init__(parent)
        self.input_guard_ms = input_guard_ms
        self.notify = notify                     # optional callable(title, message), e.g. a tray balloon
        self.active_dialog: ConfirmDialog | None = None
        self._show_requested.connect(self._show)
        self._close_requested.connect(self._close_active)

    def confirm(self, req: ConfirmRequest) -> Confirmation:
        if QThread.currentThread() is self.thread():        # already on the GUI thread: nested modal loop
            dlg = ConfirmDialog(req, self.input_guard_ms)
            self._announce(req)
            dlg.present()
            dlg.exec()
            return dlg.result_value or Confirmation(False, "dialog", "declined")
        pending = _Pending(req)
        self._show_requested.emit(pending)
        if not pending.done.wait(req.timeout + 15):          # the dialog times itself out; this is only a backstop
            self._close_requested.emit()
            return Confirmation(False, "none", "timeout")
        return pending.result

    def _announce(self, req: ConfirmRequest) -> None:
        if self.notify:
            try:
                self.notify("Vex needs your approval", req.title)
            except Exception:  # noqa: BLE001
                pass

    @Slot(object)
    def _show(self, pending: _Pending) -> None:
        dlg = ConfirmDialog(pending.req, self.input_guard_ms)
        self.active_dialog = dlg

        def finished(_code: int = 0) -> None:
            pending.result = dlg.result_value or Confirmation(False, "dialog", "declined")
            if self.active_dialog is dlg:
                self.active_dialog = None
            pending.done.set()
            dlg.deleteLater()

        dlg.finished.connect(finished)
        self._announce(pending.req)
        dlg.present()

    @Slot()
    def _close_active(self) -> None:
        if self.active_dialog is not None:
            self.active_dialog.reject()
