"""Vex desktop app: tray + visual + hotkey + native confirmations, wired to the agent.

Run with `python main.py`. (`python main.py --text` is the terminal chat.)
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("vex.app")


def pretty_combo(combo: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.replace("windows", "win").split("+"))


def run(on_ready=None) -> int:
    """Start the app. `on_ready(parts: dict)` (optional) is called once everything is built, before the event loop starts."""
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    import config
    from brain.agent import Agent
    from safety.executor import build_default_executor
    from tools import reminders
    from ui.capture import TextPromptCapture
    from ui.confirm_dialog import QtConfirmer
    from ui.controller import VexController
    from ui.hotkey import GlobalHotkey
    from ui.pill import QtPillFrontend
    from ui.speaker import SimulatedSpeaker
    from ui.tray import TrayController

    app = QApplication(sys.argv)
    app.setApplicationName("Vex")
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(Path(tempfile.gettempdir()) / "vex-app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, "Vex", "Vex is already running (look for its icon in the tray).")
        return 1

    holder: dict = {}
    confirmer = QtConfirmer(notify=lambda title, msg: holder["tray"].notify(title, msg) if "tray" in holder else None)
    executor = build_default_executor(confirmer)
    try:
        agent = Agent(executor=executor)
    except RuntimeError as e:
        QMessageBox.critical(None, "Vex can't start", str(e))
        return 1

    ui_cfg = config.settings["ui"]
    controller_ref: dict = {}
    frontend = QtPillFrontend(on_click=lambda src: controller_ref["c"].start_listening(src),
                              caption_seconds=float(ui_cfg["caption_seconds"]))
    controller = VexController(agent, frontend, SimulatedSpeaker(), TextPromptCapture())
    controller_ref["c"] = controller

    def cleanup() -> None:
        hotkey.stop()
        controller.shutdown()
        reminders.stop()
        lock.unlock()

    tray = TrayController(app, controller, frontend, executor, on_quit=cleanup, hotkey_text=pretty_combo(ui_cfg["hotkey"]))
    holder["tray"] = tray
    hotkey = GlobalHotkey(ui_cfg["hotkey"], controller.start_listening, on_error=tray.notify)
    tray.show()
    frontend.show()
    tray.set_hotkey_status(hotkey.start(), pretty_combo(ui_cfg["hotkey"]))
    reminders.start()
    log.info("Vex is running")
    if on_ready is not None:
        on_ready({"app": app, "controller": controller, "frontend": frontend, "tray": tray, "executor": executor,
                  "confirmer": confirmer, "agent": agent, "hotkey": hotkey, "capture": controller.capture})
    code = app.exec()
    cleanup()
    return code
