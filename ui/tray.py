"""System tray icon and menu.

Items: status, Listen now, Show Vex, Mute mic / pause listening, Wake word, Start with Windows, Dry run, Open memory, Open logs, Quit.
The icon shows whether Vex is listening (orb with a green dot) or muted (grey orb with a red bar). Toggles act immediately; "Wake word" and
"Start with Windows" are remembered across restarts, the others last for the session. The menu works even if the
global hotkey could not be registered.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import config
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

log = logging.getLogger("vex.tray")

STATUS = {"idle": "Ready", "listening": "Listening…", "thinking": "Thinking…", "speaking": "Speaking",
          "followup": "Still listening (no wake word needed)…"}


def make_icon(muted: bool = False) -> QIcon:
    """A small orb drawn in code (rust and lavender), so no image file is needed. Listening: a green dot. Muted: grey with a red bar."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QRadialGradient(26, 24, 40)
    if muted:
        g.setColorAt(0.0, QColor("#9a9a9a"))
        g.setColorAt(0.55, QColor("#5c5c5c"))
        g.setColorAt(1.0, QColor("#1e1e1e"))
    else:
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
    if muted:
        pen = QPen(QColor("#ff3b30"), 7, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(14, 50, 50, 14)
    else:
        p.setPen(QPen(QColor("#0b2a14"), 2))
        p.setBrush(QColor("#34c759"))
        p.drawEllipse(40, 40, 20, 20)
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    _state_sig = Signal(str)
    _health_sig = Signal(str, str)                                  # (summary, report path) from the health check thread
    _mute_sig = Signal()                                            # refresh the mute icon from any thread

    def __init__(self, app: QApplication, controller, frontend, executor, *, config_path: Path | None = None,
                 on_quit: Callable[[], None] | None = None, hotkey_text: str = "Ctrl+Alt+V", parent=None, autostart_api=None):
        super().__init__(parent)
        self.app = app
        self.controller = controller
        self.frontend = frontend
        self.executor = executor
        self.config_path = config_path
        self.on_quit = on_quit
        self.on_open_memory: Callable[[], None] | None = None       # set by the app: opens the memory viewer window
        self.on_health: Callable[[], tuple[str, str]] | None = None  # set by the app: runs the health check, returns (summary, report path)
        self._health_running = False
        self.visible = True
        self.quiet_until = 0.0                                      # monotonic time; until then ordinary notifications are only logged
        if autostart_api is None:
            from ui import autostart as autostart_api
        self.autostart = autostart_api
        self.icon_listening, self.icon_muted = make_icon(False), make_icon(True)

        self.tray = QSystemTrayIcon(self.icon_muted if controller.mic_muted else self.icon_listening, self)
        self.menu = QMenu()
        self.actions: dict[str, QAction] = {}

        self.status_action = self._add("status", "Vex: Ready", enabled=False)
        self.hotkey_action = self._add("hotkey", f"Hotkey {hotkey_text}", enabled=False)
        self.wakeword_action = self._add("wakeword_status", "Wake word: not available", enabled=False)
        self._add("listen", "Listen now", self._listen)
        self._add("type", "Type a command", self._type_command)
        self.menu.addSeparator()
        self._add("show", "Show Vex", self._toggle_visual, checked=True)
        self._add("mute", "Mute mic / pause listening", self._toggle_mic, checked=controller.mic_muted)
        self._add("wakeword", "Wake word", self._toggle_wakeword, checked=bool(config.settings["wakeword"]["enabled"]))
        self._add("followup", "Follow-up listening", self._toggle_followup,
                  checked=bool(getattr(controller, "followup_enabled", config.settings["voice"].get("followup_enabled", True))))
        self._add("autostart", "Start with Windows", self._toggle_autostart, checked=self._autostart_on())
        self._add("dry_run", "Dry run (change nothing)", self._toggle_dry_run, checked=bool(executor.dry_run))
        self.menu.addSeparator()
        self._add("health", "Health check", self._health)
        self._add("memory", "Open memory", self._open_memory)
        self._add("logs", "Open logs", self._open_logs)
        self.menu.addSeparator()
        self._add("quit", "Quit Vex", self._quit)

        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Vex")
        self.tray.activated.connect(self._activated)
        self._state_sig.connect(self._on_state)
        self._health_sig.connect(self._health_done)
        self._mute_sig.connect(self.refresh_mute_icon)
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

    def set_quiet(self, seconds: float) -> None:
        """A quiet start (autostart): ordinary notifications are only logged for this long; `notify(..., force=True)` still shows."""
        import time

        self.quiet_until = time.monotonic() + seconds

    def notify(self, title: str, message: str, force: bool = False) -> None:
        import time

        if not force and time.monotonic() < self.quiet_until:
            log.info("(quiet start) %s: %s", title, message)
            return
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
    def _autostart_on(self) -> bool:
        try:
            return bool(self.autostart.is_enabled())
        except Exception:  # noqa: BLE001
            return False

    def refresh_mute_icon_threadsafe(self) -> None:
        self._mute_sig.emit()

    def refresh_mute_icon(self) -> None:
        """Icon, tooltip and menu checkmark follow controller.mic_muted (also when it was changed by voice or a hook)."""
        muted = bool(self.controller.mic_muted)
        self.tray.setIcon(self.icon_muted if muted else self.icon_listening)
        act = self.actions.get("mute")
        if act is not None and act.isChecked() != muted:
            act.blockSignals(True)
            act.setChecked(muted)
            act.blockSignals(False)
        self._on_state(self.controller.state)

    def _on_state(self, state: str) -> None:
        if self.controller.mic_muted:
            self.status_action.setText("Vex: MUTED (not listening)")
            self.tray.setToolTip("Vex: muted, not listening")
            return
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
        self.refresh_mute_icon()
        self.notify("Microphone", "Muted: Vex isn't listening (no wake word, no hotkey recording)." if checked else "Listening again.")

    def _toggle_autostart(self, checked: bool) -> None:
        try:
            if checked:
                self.autostart.enable()
                self.notify("Start with Windows", "On: Vex starts quietly when you log in.")
            elif self.autostart.disable():
                self.notify("Start with Windows", "Off. Vex won't start with Windows, and won't turn this on again by itself.")
            else:
                self.notify("Start with Windows", "Couldn't remove the startup entry. Run: python -m ui.autostart --off")
        except Exception as e:  # noqa: BLE001
            log.exception("autostart toggle failed")
            self.notify("Start with Windows", f"That didn't work: {e}")
        self.actions["autostart"].blockSignals(True)
        self.actions["autostart"].setChecked(self._autostart_on())
        self.actions["autostart"].blockSignals(False)

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

    def _toggle_followup(self, checked: bool) -> None:
        self.controller.followup_enabled = checked
        try:
            config.set_toml_value("voice", "followup_enabled", checked, path=self.config_path)
        except OSError as e:
            log.warning("could not save the follow-up setting: %s", e)
        self.notify("Follow-up listening", "On: after a reply Vex keeps listening for a few seconds, no wake word needed." if checked
                    else "Off: say \"Hey Vex\" (or press the hotkey) before every command.")

    def _toggle_dry_run(self, checked: bool) -> None:
        self.executor.set_dry_run(checked)
        self.notify("Dry run", "On: tools only report what they would do." if checked else "Off: tools act for real.")

    def _open_memory(self) -> None:
        if self.on_open_memory is not None:
            try:
                self.on_open_memory()
                return
            except Exception:  # noqa: BLE001
                log.exception("memory viewer failed")
                self.notify("Memory", "The memory viewer couldn't open. Opening the notes folder instead.")
        self._open_path(config.ROOT / config.settings["paths"]["vault"])

    def _health(self) -> None:
        """Run the free health check in the background; a toast with the verdict, then the report opens."""
        if self.on_health is None or self._health_running:
            return
        self._health_running = True
        self.notify("Health check", "Checking every part of Vex (about 10 seconds, no API quota used)…", force=True)

        def work() -> None:
            try:
                summary, path = self.on_health()
            except Exception as e:  # noqa: BLE001
                log.exception("health check failed")
                summary, path = f"The health check itself failed: {type(e).__name__}", ""
            self._health_sig.emit(summary, path)

        import threading

        threading.Thread(target=work, name="health-check", daemon=True).start()

    def _health_done(self, summary: str, path: str) -> None:
        self._health_running = False
        self.notify("Health check", summary + (" Report: logs\\health.txt" if path else ""), force=True)
        if path:
            self._open_path(Path(path))

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
