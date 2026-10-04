"""Vex desktop app: tray + visual + hotkey + native confirmations + voice, wired to the agent.

Run with `python main.py`. (`python main.py --text` is the terminal chat.)
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from voice.timeline import timeline

log = logging.getLogger("vex.app")


def pretty_combo(combo: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.lower().replace("windows", "win").split("+"))


def open_control_center() -> bool:
    """Start the Control Center (python -m vex_app), detached. If it is already open it just comes to the front."""
    import subprocess

    import config

    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "vex_app"], cwd=str(config.ROOT), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        log.info("opening the Control Center")
        return True
    except OSError:
        log.exception("could not start the Control Center")
        return False


def build_visual(ui_cfg: dict, pill, on_click, notify, on_open=None):
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
        clicks = ClickListener(ui_cfg["orb_click_port"], on_click, on_open=on_open)
        clicks.start()
    except OSError as e:                                         # port taken: the orb still works, just without click-to-talk
        log.warning("click port %s unavailable (%s); clicking the orb won't start listening", ui_cfg["orb_click_port"], e)
    orb_front = OrbFrontend(ui_cfg["orb_port"])
    if mode == "both":
        multi = MultiFrontend([orb_front, pill])
    else:
        pill.hide()
        multi = MultiFrontend([orb_front, NullFrontend()])

    def orb_gave_up() -> None:                                   # the orb keeps dying: the small built-in pill takes over, and Vex says so
        log.error("the orb crashed repeatedly; switching to the built-in pill")
        multi.frontends = [f for f in multi.frontends if f is not orb_front]
        if pill not in multi.frontends:
            multi.frontends.append(pill)
        pill.show()
        notify("Vex", "The orb keeps crashing, so I switched to the small pill. Everything else still works. "
                      f"Details: {config.log_file()}")

    import config

    orb.watch(orb_gave_up)
    return multi, orb, clicks


def build_wakeword(controller, stack, notify):
    """The "Hey Vex" listener, wired into the controller's single trigger path; None when voice is off."""
    import config
    from voice.wakeword import WakeWordListener, play_chime

    if stack is None:
        return None
    wcfg = config.wakeword_config()
    holder: dict = {}

    def on_wake() -> str:
        timeline.note_wake()                                                     # the latency stopwatch: the wake word was heard just now
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


class ConfirmTracker:
    """Passes confirmations through unchanged and tells the controller one was asked (no follow-up window after a confirmation)."""

    def __init__(self, inner, on_ask):
        self.inner, self.on_ask = inner, on_ask
        self.watchers: list = []                                # fn(req, answer_or_None): the local API shows prompts (display only)

    def _tell(self, req, answer) -> None:
        for fn in list(self.watchers):
            try:
                fn(req, answer)
            except Exception:  # noqa: BLE001
                log.debug("confirm watcher failed", exc_info=True)

    def confirm(self, req):
        try:
            self.on_ask()
        except Exception:  # noqa: BLE001
            log.debug("confirm tracker hook failed", exc_info=True)
        self._tell(req, None)
        answer = self.inner.confirm(req)
        self._tell(req, answer)
        return answer

    def __getattr__(self, name):
        return getattr(self.inner, name)


def app_status(controller, hotkey, wake, orb, tray) -> dict:
    """What the running Vex is doing, for the heartbeat and the health check. Never raises."""
    from tools import reminders

    def safe(fn, default=None):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default

    wake_on = safe(lambda: tray.actions["wakeword"].isChecked(), False)
    if wake is None or not wake_on:
        wake_state = "off"
    else:
        alive = safe(lambda: wake._thread is not None and wake._thread.is_alive(), False)
        wake_state = "running" if alive and getattr(wake, "running", False) else ("starting" if alive else "down")
    if orb is None:
        orb_state = "pill"
    elif safe(lambda: orb.watcher.gave_up, False):
        orb_state = "pill"
    elif not getattr(orb, "started_by_us", True):
        orb_state = "external"
    else:
        orb_state = "crashed" if safe(orb.crashed, False) else "running"
    sched = getattr(reminders, "_scheduler", None)
    return {"state": controller.state, "hotkey": bool(safe(lambda: hotkey.active, False)), "wakeword": wake_state, "orb": orb_state,
            "mic_muted": bool(controller.mic_muted), "followup": bool(getattr(controller, "followup_enabled", False)),
            "reminders": bool(sched is not None and getattr(sched, "running", False))}


def build_api(controller, executor, tray, tracker, dialog, housekeeper, status, wake=None, stack=None):
    """The local API (docs/API.md), or None if it is off or can't start. Never raises: Vex runs the same without it."""
    import config

    cfg = config.settings.get("api", {})
    if not cfg.get("enabled", True):
        return None
    try:
        from tools import memory as memory_tools
        from ui import health
        from ui.api_core import ApiCore
        from ui.api_server import ApiServer, load_or_create_token

        def health_fn() -> dict:
            checks = health.run_checks(status=status())
            redact = health.Redactor(health.secrets_from_env(config.ROOT))
            return {"summary": redact(health.summary_line(checks)),
                    "checks": [{"name": c.name, "status": c.status, "detail": redact(c.detail), "hint": redact(c.hint)} for c in checks]}

        from ui.conversations import ConversationLog

        keep = int(cfg.get("keep_conversations_days", 14))
        conversations = ConversationLog(config.ROOT / "data" / "conversations.jsonl", keep_days=keep)
        from ui.voice_control import VoiceControl

        core = ApiCore(controller=controller, executor=executor, tray=tray, memory_factory=memory_tools.get_memory, conversations=conversations,
                       voice_control=VoiceControl(controller, wake=wake, stack=stack), restart_fn=tray.restart_vex,
                       dialog_confirmer=dialog, housekeeper=housekeeper, health_fn=health_fn, status_fn=status,
                       hotkey=pretty_combo(config.settings["ui"]["hotkey"]))
        core.attach()
        tracker.watchers.append(core.on_confirm)
        server = ApiServer(core, load_or_create_token(config.ROOT / "data" / "api_token"), port=int(cfg.get("port", 47480)),
                           allowed_origins=list(cfg.get("allowed_origins", [])))
        return server if server.start() else None
    except Exception:  # noqa: BLE001
        log.exception("local API could not start; Vex runs on without it")
        return None


QUIET_SECONDS = 90.0
WATCHDOG_SECONDS = 30
MAX_RESTARTS = 3


def build_watches(hotkey, hotkey_ok: bool, wake, tray, controller) -> list:
    """Watches that bring a dead hotkey thread or wake word listener back (3 times in 10 minutes, then one message)."""
    import config
    from ui.recovery import ComponentWatch

    def giveup(name: str) -> None:
        tray.notify("Vex", f"{name} keeps failing, so I've stopped retrying it. Everything else still works. Details: {config.log_file()}",
                    force=True)

    watches = [ComponentWatch("the hotkey", lambda: hotkey.active, hotkey.start, giveup, wanted=lambda: hotkey_ok)]
    if wake is not None:
        def wake_healthy() -> bool:
            t = getattr(wake, "_thread", None)
            return t is not None and t.is_alive()

        watches.append(ComponentWatch("the wake word listener", wake_healthy, wake.start, giveup,
                                      wanted=lambda: tray.actions["wakeword"].isChecked() and not controller.mic_muted))
    return watches


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


def build_curator(controller, vault_full=None):
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
                      is_busy=lambda: controller.state != "idle", vault_full=vault_full)
        controller.on_turn.append(cur.add_turn)
        controller.on_interaction_end.append(cur.interaction_ended)
        cur.start()
        log.info("memory curator on (conversation over after %s s idle; audit: logs/curator.jsonl)", cfg["curator_idle_seconds"])
        return cur
    except Exception:  # noqa: BLE001 - memory is a bonus; Vex must start without it
        log.exception("memory curator could not start")
        return None


def build_consolidator(controller):
    """The nightly memory tidy-up, or None if it is off or can't start. Never raises."""
    import config

    if not config.settings["memory"]["consolidate"]:
        return None
    try:
        from memory.consolidate import Consolidator
        from tools import memory as memory_tools

        con = Consolidator(memory_tools.get_memory(), is_busy=lambda: controller.state != "idle",
                           log_path=config.ROOT / config.settings["paths"]["logs"] / "memory_consolidation.jsonl")
        con.start()
        return con
    except Exception:  # noqa: BLE001
        log.exception("memory consolidation could not start")
        return None


def run(on_ready=None, console: bool = False, quiet: bool = False, restarted: int = 0) -> int:
    """Start the app. `on_ready(parts: dict)` (optional) is called once everything is built, before the event loop starts.

    quiet: a start from Windows autostart: tray only, no toasts for the first minute (except `restarted` and the one-time autostart notice).
    restarted: the supervisor restarted Vex after a crash; one toast says so."""
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

    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)           # needed once the memory viewer (QtWebEngine) opens
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
    def open_cc_from_pill() -> None:
        if "controller" in holder:
            holder["controller"].cancel_capture()          # the double-click's first click started listening: not wanted
        open_control_center()

    frontend, orb, clicks = build_visual(ui_cfg, pill, lambda src: holder["controller"].start_listening(src), notify,
                                         on_open=open_cc_from_pill)
    typed = TextPromptCapture()

    # voice: recorder + Whisper + TTS chain; the typed prompt stays available as the fallback and from the tray
    stack = None
    if voice_cfg["enabled"]:
        from voice.factory import build_voice

        stack = build_voice(on_level=frontend.set_level, notify=notify, typed_fallback=typed)
    dialog = QtConfirmer(notify=notify)
    confirmer = (stack.wrap_confirmer(dialog, is_muted=lambda: holder["controller"].mic_muted, on_level=frontend.set_level)
                 if stack else dialog)

    tracker = ConfirmTracker(confirmer, lambda: holder["controller"].note_confirmation() if "controller" in holder else None)
    executor = build_default_executor(tracker)
    from tools import registry as tool_registry

    tool_registry.load_disabled()                            # tools you switched off stay off (data/tools_disabled.json)
    try:
        agent = Agent(executor=executor)
    except RuntimeError as e:
        QMessageBox.critical(None, "Vex can't start", str(e))
        return 1

    controller = VexController(agent, frontend, stack.speaker if stack else SimulatedSpeaker(),
                               stack.capture if stack else typed, typed_capture=typed)
    holder["controller"] = controller
    controller.followup_enabled = bool(voice_cfg.get("followup_enabled", True))
    controller.followup_seconds = float(voice_cfg.get("followup_s", 8))
    controller.followup_min_speech_ms = int(voice_cfg.get("followup_min_speech_ms", 250))
    controller.followup_delay_s = float(voice_cfg.get("followup_delay_ms", 300)) / 1000
    controller.wake_greeting = str(voice_cfg.get("wake_greeting", "What can I do for you?"))
    controller.wake_wait_s = float(voice_cfg.get("wake_wait_s", 3.0))
    controller.filler_text = str(voice_cfg.get("filler_text", "One sec…"))
    controller.filler_after_s = float(voice_cfg.get("filler_after_s", 2.5))
    timeline.configure(config.ROOT / config.settings["paths"]["logs"] / "latency.jsonl")
    from voice.chime import play_reminder_chime

    controller.chime = play_reminder_chime
    reminders.set_announcer(controller.announce)             # a reminder is spoken (chime, orb, voice), not just a toast
    from ui.housekeeping import Housekeeper

    housekeeper = Housekeeper(config.ROOT, config.ROOT / config.settings["paths"]["logs"], config.ROOT / config.settings["paths"]["vault"],
                              notify=notify)
    housekeeper.start()                                      # 2 minutes after start, then daily: logs, archives, backups, vault cap
    curator = build_curator(controller, vault_full=housekeeper.vault_full)
    consolidator = build_consolidator(controller)

    def cleanup() -> None:
        housekeeper.stop()
        if holder.get("api") is not None:
            holder["api"].stop()
        if curator is not None:
            curator.stop()
        if consolidator is not None:
            consolidator.stop()
        if wake is not None:
            wake.stop()
        hotkey.stop()
        controller.shutdown()
        reminders.set_announcer(None)
        reminders.stop()
        if clicks is not None:
            clicks.stop()
        if orb is not None:
            orb.stop()

    tray = TrayController(app, controller, frontend, executor, on_quit=cleanup, hotkey_text=pretty_combo(ui_cfg["hotkey"]))
    holder["tray"] = tray
    if quiet:
        tray.set_quiet(QUIET_SECONDS)
    controller.on_mute_change.append(tray.refresh_mute_icon_threadsafe)

    def open_viewer() -> None:
        if "viewer" not in holder:
            from memory.viewer_service import ViewerService
            from tools import memory as memory_tools
            from ui.memory_viewer import MemoryViewerWindow

            holder["viewer"] = MemoryViewerWindow(ViewerService(memory_tools.get_memory()))
        holder["viewer"].show_and_raise()

    tray.on_open_memory = open_viewer
    tray.on_open_control_center = open_control_center
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
    hotkey_ok = hotkey.start()
    tray.set_hotkey_status(hotkey_ok, pretty_combo(ui_cfg["hotkey"]))
    reminders.start()
    if wake is not None:
        import config as _config

        on = bool(_config.wakeword_config()["enabled"])
        tray.set_wakeword_status(wake.label, on)
        if on:
            wake.start()

        def on_mute_change() -> None:                            # muted = the microphone is released entirely (no wake word stream either)
            if controller.mic_muted:
                wake.stop()
            elif tray.actions["wakeword"].isChecked():
                wake.start()

        controller.on_mute_change.append(on_mute_change)
    watches = build_watches(hotkey, hotkey_ok, wake, tray, controller)
    from ui import health

    heartbeat_path = config.ROOT / "data" / "status.json"

    def status() -> dict:
        return app_status(controller, hotkey, wake, orb, tray)

    def tick() -> None:
        for w in watches:
            w.check()
        health.write_heartbeat(heartbeat_path, status())            # the health check (and you) can see what the running Vex is doing

    def run_health() -> tuple[str, str]:
        checks = health.run_checks(status=status())
        text = health.render(checks, False, health.Redactor(health.secrets_from_env(config.ROOT)))
        path = health.write_report(text, config.ROOT / config.settings["paths"]["logs"] / "health.txt")
        return health.summary_line(checks), str(path)

    tray.on_health = run_health
    api = build_api(controller, executor, tray, tracker, dialog, housekeeper, status, wake=wake, stack=stack)
    holder["api"] = api
    controller.listeners.append(lambda st: st == "idle" and health.write_heartbeat(heartbeat_path, status()))   # never a stale "followup"
    watchdog = QTimer()
    watchdog.timeout.connect(tick)
    QTimer.singleShot(3000, lambda: health.write_heartbeat(heartbeat_path, status()))
    watchdog.start(WATCHDOG_SECONDS * 1000)
    holder["watchdog"] = watchdog
    from ui import autostart

    try:
        if autostart.ensure_default() == "enabled":
            tray.notify("Vex starts with Windows now",
                        "From now on Vex starts quietly when you log in. Turn it off any time: tray > Start with Windows.", force=True)
    except Exception:  # noqa: BLE001 - never block startup over this
        log.exception("autostart setup failed")
    if restarted:
        tray.notify("Vex restarted", f"Vex had a problem and was restarted ({restarted} of {MAX_RESTARTS} allowed in 10 minutes). "
                                     f"Details: {config.log_file()}", force=True)
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
