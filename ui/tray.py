"""System tray icon and menu.

Items: status, Listen now, Show Vex, Mute microphone, Wake word, Dry run, Open memory (placeholder), Open logs, Quit.
Toggles act immediately; "Wake word" is saved to config.toml, the others last for the session. The menu works even if the
global hotkey could not be registered.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import config
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

log = logging.getLogger("vex.tray")

STATUS = {"idle": "Ready", "listening": "Listening…", "thinking": "Thinking…", "speaking": "Speaking"}


def make_icon() -> QIcon:
    """A small orb drawn in code (rust and lavender), so no image file is needed."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QRadialGradient(26, 24, 40)
    g.setColorAt(0.0, QColor("#f27052"))
    g.setColorAt(0.55, QColor("#bc3e28"))
    g.setColorAt(1.0, QColor("#2a0d09"))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    h = QRadialGradient(44, 40, 22)
    h.setColorAt(0.0, QColor(150, 156, 246, 200))
    h.setColorAt(1.0, QColor(150, 156, 246, 0))
    p.setBrush(h)
    p.drawEllipse(4, 4, 56, 56)
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    _state_sig = Signal(str)

    def __init__(self, app: QApplication, controller, frontend, executor, *, config_path: Path | None = None,
                 on_quit: Callable[[], None] | None = None, hotkey_text: str = "Ctrl+Alt+V", parent=None):
        super().__init__(parent)
        self.app = app
        self.controller = controller
        self.frontend = frontend
        self.executor = executor
        self.config_path = config_path
        self.on_quit = on_quit
        self.visible = True

        self.tray = QSystemTrayIcon(make_icon(), self)
        self.menu = QMenu()
        self.actions: dict[str, QAction] = {}

        self.status_action = self._add("status", "Vex: Ready", enabled=False)
        self.hotkey_action = self._add("hotkey", f"Hotkey {hotkey_text}", enabled=False)
        self.wakeword_action = self._add("wakeword_status", "Wake word: not available", enabled=False)
        self._add("listen", "Listen now", self._listen)
        self._add("type", "Type a command", self._type_command)
        self.menu.addSeparator()
        self._add("show", "Show Vex", self._toggle_visual, checked=True)
        self._add("mute", "Mute microphone", self._toggle_mic, checked=controller.mic_muted)
        self._add("wakeword", "Wake word", self._toggle_wakeword, checked=bool(config.settings["wakeword"]["enabled"]))
        self._add("dry_run", "Dry run (change nothing)", self._toggle_dry_run, checked=bool(executor.dry_run))
        self.menu.addSeparator()
        self._add("memory", "Open memory (viewer coming soon)", self._open_memory)
        self._add("logs", "Open logs", self._open_logs)
        self.menu.addSeparator()
        self._add("quit", "Quit Vex", self._quit)

        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Vex")
        self.tray.activated.connect(self._activated)
        self._state_sig.connect(self._on_state)
        controller.listeners.append(self._state_sig.emit)       # states arrive from worker threads; the signal marshals

    # ---- construction helpers --------------------------------------------------------------------------------------------
    def _add(self, key: str, text: str, slot=None, *, checked: bool | None = None, enabled: bool = True) -> QAction:
        act = QAction(text, self.menu)
        act.setEnabled(enabled)
        if checked is not None:
            act.setCheckable(True)
            act.setChecked(checked)
            if slot:
                act.toggled.connect(slot)
        elif slot:
            act.triggered.connect(lambda _checked=False, s=slot: s())
        self.menu.addAction(act)
        self.actions[key] = act
        return act

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def notify(self, title: str, message: str) -> None:
        if QSystemTrayIcon.supportsMessages() and self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, 6000)
        else:
            log.info("%s: %s", title, message)

    def set_wakeword_status(self, label: str, on: bool) -> None:
        """Show which model is active ("Hey Vex" or "Hey Jarvis (fallback)") and whether the listener is on."""
        self.wakeword_action.setText(f"Wake word: {label}" if on else f"Wake word: off ({label})")
        act = self.actions.get("wakeword")
        if act is not None and act.isChecked() != on:
            act.setChecked(on)

    def set_hotkey_status(self, active: bool, combo: str) -> None:
        self.hotkey_action.setText(f"Hotkey {combo}" + ("" if active else " (unavailable)"))

    # ---- handlers --------------------------------------------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        self.status_action.setText(f"Vex: {STATUS.get(state, state)}")
        self.tray.setToolTip(f"Vex: {STATUS.get(state, state)}")

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self._listen()

    def _listen(self) -> None:
        self.controller.start_listening("tray")

    def _type_command(self) -> None:
        self.controller.start_listening("tray", typed=True)

    def _toggle_visual(self, checked: bool) -> None:
        self.visible = checked
        (self.frontend.show if checked else self.frontend.hide)()

    def _toggle_mic(self, checked: bool) -> None:
        self.controller.set_mic_muted(checked)
        self.notify("Microphone", "Muted: Vex won't listen." if checked else "Unmuted.")

    def _toggle_wakeword(self, checked: bool) -> None:
        try:
            config.set_toml_value("wakeword", "enabled", checked, path=self.config_path)
        except OSError as e:
            log.warning("could not save the wake word setting: %s", e)
            self.notify("Wake word", f"Couldn't save that setting: {e}")
        for cb in list(getattr(self.controller, "on_wakeword_toggle", [])):
            try:
                cb(checked)
            except Exception:  # noqa: BLE001
                log.exception("wake word toggle hook failed")

    def _toggle_dry_run(self, checked: bool) -> None:
        self.executor.set_dry_run(checked)
        self.notify("Dry run", "On: tools only report what they would do." if checked else "Off: tools act for real.")

    def _open_memory(self) -> None:
        self.notify("Memory", "The memory viewer isn't built yet. Opening the notes folder instead.")
        self._open_path(config.ROOT / config.settings["paths"]["vault"])

    def _open_logs(self) -> None:
        self._open_path(config.ROOT / config.settings["paths"]["logs"])

    def _open_path(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)  # noqa: S606
        except OSError as e:
            self.notify("Couldn't open folder", str(e))

    def _quit(self) -> None:
        self.tray.hide()
        if self.on_quit:
            self.on_quit()
        self.app.quit()
