"""Vex desktop app: tray + visual + hotkey + native confirmations + voice, wired to the agent.

Run with `python main.py`. (`python main.py --text` is the terminal chat.)
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("vex.app")


def pretty_combo(combo: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.lower().replace("windows", "win").split("+"))


def build_visual(ui_cfg: dict, pill, on_click, notify):
    """The visual(s) to drive: the orb app (default), the Qt pill, or both. Falls back to the pill if the orb can't start.

    Returns (frontend, OrbProcess | None, ClickListener | None).
    """
    from config import ROOT
    from ui.frontend import NullFrontend
    from ui.orb_frontend import ClickListener, MultiFrontend, OrbFrontend, OrbProcess

    mode = ui_cfg["visual"]
    if mode == "pill":
        return pill, None, None
    orb = OrbProcess(ROOT / ui_cfg["orb_dir"], port=ui_cfg["orb_port"], click_port=ui_cfg["orb_click_port"],
                     position=ui_cfg["orb_position"], palette=ui_cfg["orb_palette"], size=ui_cfg["orb_size"],
                     autosleep=ui_cfg["orb_autosleep"], mode=ui_cfg["orb_mode"], edge=ui_cfg["orb_edge"],
                     edge_y=ui_cfg["orb_edge_y"], center_y=ui_cfg["orb_center_y"], orb_radius=ui_cfg["orb_radius"])
    if not orb.start():
        notify("Vex", "The orb couldn't start, so I'm using the small pill instead. See docs/ORB.md.")
        return pill, None, None
    clicks = None
    try:
        clicks = ClickListener(ui_cfg["orb_click_port"], on_click)
        clicks.start()
    except OSError as e:                                         # port taken: the orb still works, just without click-to-talk
        log.warning("click port %s unavailable (%s); clicking the orb won't start listening", ui_cfg["orb_click_port"], e)
    orb_front = OrbFrontend(ui_cfg["orb_port"])
    if mode == "both":
        return MultiFrontend([orb_front, pill]), orb, clicks
    pill.hide()
    return MultiFrontend([orb_front, NullFrontend()]), orb, clicks


def build_wakeword(controller, stack, notify):
    """The "Hey Vex" listener, wired into the controller's single trigger path; None when voice is off."""
    import config
    from voice.wakeword import WakeWordListener, play_chime

    if stack is None:
        return None
    wcfg = config.wakeword_config()
    holder: dict = {}

    def on_wake() -> str:
        if controller.mic_muted:
            return controller.start_listening("wakeword")                       # says "The microphone is muted." and returns "muted"
        if wcfg["chime"]:
            play_chime(blocking=False)                                           # a soft blip; the listener keeps hearing meanwhile
        stack.recorder.seed = holder["listener"].take_audio                      # words said right after the wake phrase, fetched when recording starts
        result = controller.start_listening("wakeword")
        if result not in ("started", "barge_in"):
            stack.recorder.seed = None
        return result

    listener = WakeWordListener(on_wake, model_path=wcfg["model_path"], fallback_model=wcfg["fallback_model"],
                                threshold=wcfg["threshold"], cooldown=wcfg["cooldown"], vad_threshold=wcfg["vad_threshold"],
                                device=wcfg["input_device"] or stack.recorder.device, notify=notify)
    holder["listener"] = listener
    controller.on_interaction_start.append(listener.pause)                      # every turn, whatever started it
    controller.on_interaction_end.append(listener.resume)                       # after the reply has finished speaking
    return listener


def startup_summary(parts: dict, hotkey_text: str, console: bool) -> list[str]:
    """What was started, in plain words (printed at launch and logged), so a silent failure is never silent."""
    import config
    from ui.orb_frontend import OrbProcess

    hk, stack, orb, pill = parts["hotkey"], parts["voice"], parts["orb"], parts["pill"]
    wake = parts.get("wakeword")
    ui_cfg = config.settings["ui"]
    lines = ["Vex is running."]
    lines.append(f"  Hotkey {hotkey_text}: " + ("registered" if hk.active else f"NOT available ({hk.error})"))
    if orb is not None:
        how = "started by Vex" if orb.started_by_us else "already running"
        lines.append(f"  Orb: {how} (UDP {ui_cfg['orb_port']}, click-back on {ui_cfg['orb_click_port']})")
    else:
        lines.append("  Visual: built-in pill (the orb is off or could not start)")
    if stack is not None:
        providers = [p.name + ("" if p.available() else " (not configured)") for p in stack.chain.providers]
        mic = stack.recorder.device or "default microphone"
        from voice.micstate import default_mic_muted
        if default_mic_muted():
            mic += " (MUTED in Windows: unmute it or Vex won't hear you)"
            log.warning("the Windows default microphone is muted")
        lines.append(f"  Voice: on | mic: {mic} | speech recognition: Groq {stack.stt.model} ({'ready' if stack.stt.available else 'NO KEY'}) "
                     f"| speech: {', '.join(providers)}")
    else:
        lines.append("  Voice: off (typing only)")
    if wake is not None:
        lines.append(f"  Wake word: {wake.label}" + (" (starting)" if config.wakeword_config()["enabled"] else " (off; tray > Wake word)"))
    lines.append(f"  Tray icon: bottom-right of the taskbar (Listen now / Type a command / Quit)")
    lines.append(f"  Log file: {config.log_file()}")
    lines.append("  Stop it from the tray menu (Quit Vex)" + (" or press Ctrl+C here." if console else "."))
    return lines


def build_curator(controller):
    """The memory curator (saves durable facts after a conversation), or None if it is off or can't start. Never raises."""
    import config

    cfg = config.settings["memory"]
    if not cfg["curator"]:
        return None
    try:
        from brain.llm import build_client
        from memory.curator import Curator
        from tools import memory as memory_tools

        cur = Curator(memory_tools.get_memory(), llm_factory=lambda: build_client(order=cfg["curator_order"]),
                      idle_seconds=float(cfg["curator_idle_seconds"]), min_words=int(cfg["curator_min_words"]),
                      max_attempts=int(cfg["curator_max_attempts"]), log_path=config.ROOT / config.settings["paths"]["logs"] / "curator.jsonl",
                      is_busy=lambda: controller.state != "idle")
        controller.on_turn.append(cur.add_turn)
        controller.on_interaction_end.append(cur.interaction_ended)
        cur.start()
        log.info("memory curator on (conversation over after %s s idle; audit: logs/curator.jsonl)", cfg["curator_idle_seconds"])
        return cur
    except Exception:  # noqa: BLE001 - memory is a bonus; Vex must start without it
        log.exception("memory curator could not start")
        return None


def run(on_ready=None, console: bool = False) -> int:
    """Start the app. `on_ready(parts: dict)` (optional) is called once everything is built, before the event loop starts."""
    import signal

    from PySide6.QtCore import QTimer
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

    signal.signal(signal.SIGINT, lambda *_: app.quit())              # Ctrl+C in the terminal quits cleanly...
    sigint_pump = QTimer()
    sigint_pump.start(250)                                           # ...once Python gets a chance to run between Qt events
    sigint_pump.timeout.connect(lambda: None)

    holder: dict = {}
    ui_cfg, voice_cfg = config.settings["ui"], config.settings["voice"]

    def notify(title: str, message: str) -> None:
        if "tray" in holder:
            holder["tray"].notify(title, message)

    pill = QtPillFrontend(on_click=lambda src: holder["controller"].start_listening(src),
                          caption_seconds=float(ui_cfg["caption_seconds"]))
    frontend, orb, clicks = build_visual(ui_cfg, pill, lambda src: holder["controller"].start_listening(src), notify)
    typed = TextPromptCapture()

    # voice: recorder + Whisper + TTS chain; the typed prompt stays available as the fallback and from the tray
    stack = None
    if voice_cfg["enabled"]:
        from voice.factory import build_voice

        stack = build_voice(on_level=frontend.set_level, notify=notify, typed_fallback=typed)
    dialog = QtConfirmer(notify=notify)
    confirmer = (stack.wrap_confirmer(dialog, is_muted=lambda: holder["controller"].mic_muted, on_level=frontend.set_level)
                 if stack else dialog)

    executor = build_default_executor(confirmer)
    try:
        agent = Agent(executor=executor)
    except RuntimeError as e:
        QMessageBox.critical(None, "Vex can't start", str(e))
        return 1

    controller = VexController(agent, frontend, stack.speaker if stack else SimulatedSpeaker(),
                               stack.capture if stack else typed, typed_capture=typed)
    holder["controller"] = controller
    curator = build_curator(controller)

    def cleanup() -> None:
        if curator is not None:
            curator.stop()
        if wake is not None:
            wake.stop()
        hotkey.stop()
        controller.shutdown()
        reminders.stop()
        if clicks is not None:
            clicks.stop()
        if orb is not None:
            orb.stop()

    tray = TrayController(app, controller, frontend, executor, on_quit=cleanup, hotkey_text=pretty_combo(ui_cfg["hotkey"]))
    holder["tray"] = tray
    hotkey = GlobalHotkey(ui_cfg["hotkey"], controller.start_listening, on_error=tray.notify)
    wake = build_wakeword(controller, stack, tray.notify)
    if wake is not None:
        def toggle_wake(on: bool) -> None:
            (wake.start if on else wake.stop)()
            tray.set_wakeword_status(wake.label, on)

        controller.on_wakeword_toggle.append(toggle_wake)
    tray.show()
    frontend.show()
    frontend.set_state("idle")
    tray.set_hotkey_status(hotkey.start(), pretty_combo(ui_cfg["hotkey"]))
    reminders.start()
    if wake is not None:
        import config as _config

        on = bool(_config.wakeword_config()["enabled"])
        tray.set_wakeword_status(wake.label, on)
        if on:
            wake.start()
    parts = {"app": app, "controller": controller, "frontend": frontend, "tray": tray, "executor": executor,
             "confirmer": confirmer, "dialog": dialog, "agent": agent, "hotkey": hotkey, "capture": controller.capture,
             "typed": typed, "voice": stack, "orb": orb, "pill": pill, "wakeword": wake}
    summary = startup_summary(parts, pretty_combo(ui_cfg["hotkey"]), console)
    for line in summary:
        print(line, flush=True)
        log.info(line.strip())
    if on_ready is not None:
        on_ready(parts)
    code = app.exec()
    cleanup()
    return code
