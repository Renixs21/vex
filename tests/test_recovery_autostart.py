"""Step B: restart budget + watches, the supervisor's crash policy, autostart rules, orb watchdog, mute icon, quiet start."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ui_qt import qapp  # noqa: F401 - the shared QApplication fixture
from ui import autostart, supervisor
from ui.recovery import ComponentWatch, RestartBudget


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---- budget and watches -------------------------------------------------------------------------------------------------------------
def test_budget_allows_three_in_ten_minutes_then_refuses_until_they_age_out():
    c = Clock()
    b = RestartBudget(clock=c)
    assert [b.allow() for _ in range(4)] == [True, True, True, False]
    c.t = 599
    assert not b.allow()
    c.t = 601                                                   # the first three are now older than 10 minutes
    assert b.allow() and b.used == 1


def test_watch_restarts_a_dead_component_then_gives_up_once():
    state = {"alive": False, "restarts": 0, "giveups": []}
    w = ComponentWatch("the hotkey", lambda: state["alive"], lambda: state.update(restarts=state["restarts"] + 1), state["giveups"].append,
                       budget=RestartBudget(clock=Clock()))
    assert [w.check() for _ in range(5)] == ["restarted"] * 3 + ["gave_up", "idle"]
    assert state["restarts"] == 3 and state["giveups"] == ["the hotkey"]


def test_watch_does_nothing_when_healthy_or_not_wanted_and_survives_errors():
    calls = []
    ok = ComponentWatch("a", lambda: True, lambda: calls.append(1), lambda n: None)
    off = ComponentWatch("b", lambda: False, lambda: calls.append(2), lambda n: None, wanted=lambda: False)

    def boom():
        raise RuntimeError("x")

    broken = ComponentWatch("c", boom, lambda: calls.append(3), lambda n: None)
    assert (ok.check(), off.check(), broken.check()) == ("ok", "idle", "ok") and calls == []
    bad_restart = ComponentWatch("d", lambda: False, boom, lambda n: None)
    assert bad_restart.check() == "restarted"                  # a failing restart is logged, never raised


# ---- supervisor ----------------------------------------------------------------------------------------------------------------------
class FakeProc:
    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


def run_supervisor(tmp_path, codes, **kw):
    started, slept, toasts = [], [], []

    def popen(cmd, **k):
        started.append(cmd)
        return FakeProc(codes.pop(0))

    rc = supervisor.run(tmp_path, tmp_path / "logs", popen=popen, sleep=slept.append, budget=RestartBudget(clock=Clock()),
                        notify=lambda t, m: toasts.append((t, m)), **kw)
    return rc, started, slept, toasts


def test_a_clean_quit_is_never_restarted(tmp_path):
    rc, started, _, toasts = run_supervisor(tmp_path, [0])
    assert rc == 0 and len(started) == 1 and toasts == []


def test_another_copy_already_running_ends_the_supervisor_quietly(tmp_path):
    rc, started, _, toasts = run_supervisor(tmp_path, [3])
    assert rc == 0 and len(started) == 1 and toasts == []


def test_a_crash_restarts_with_a_restarted_flag_and_a_later_clean_quit_ends_it(tmp_path):
    rc, started, slept, _ = run_supervisor(tmp_path, [1, 0])
    assert rc == 0 and len(started) == 2 and "--restarted" not in started[0] and started[1][-2:] == ["--restarted", "1"]
    assert supervisor.RESTART_DELAY in slept and all("--quiet" in c for c in started)


def test_three_restarts_in_ten_minutes_then_it_stops_and_the_toast_names_the_log(tmp_path):
    rc, started, _, toasts = run_supervisor(tmp_path, [1, 1, 1, 1, 1])
    assert rc == 1 and len(started) == 4                       # the first start + 3 restarts, then no fifth
    assert len(toasts) == 1 and str(tmp_path / "logs" / "vex.log") in toasts[0][1]


def test_the_startup_delay_happens_once_before_the_first_start(tmp_path):
    _, _, slept, _ = run_supervisor(tmp_path, [1, 0], startup_delay=15)
    assert slept[0] == 15 and slept.count(15) == 1


def test_a_child_that_cannot_even_be_launched_counts_as_a_crash(tmp_path):
    def popen(cmd, **k):
        raise OSError("nope")

    toasts = []
    rc = supervisor.run(tmp_path, tmp_path / "logs", popen=popen, sleep=lambda s: None, budget=RestartBudget(clock=Clock()),
                        notify=lambda t, m: toasts.append(m))
    assert rc == 1 and len(toasts) == 1


def test_child_command_is_quiet_and_flags_restarts(tmp_path):
    cmd = supervisor.child_command(tmp_path, quiet=True, restarted=2)
    assert cmd[1].endswith("main.py") and "--quiet" in cmd and cmd[-2:] == ["--restarted", "2"]
    assert "--quiet" not in supervisor.child_command(tmp_path, quiet=False)


def test_main_routes_supervise_and_reads_the_flags(monkeypatch):
    import main

    a = main.build_parser().parse_args(["--supervise", "--startup-delay", "15", "--quiet"])
    assert a.supervise and a.startup_delay == 15 and a.quiet and a.restarted == 0
    seen = {}
    monkeypatch.setattr(supervisor, "run", lambda root, logs, startup_delay, quiet: seen.update(delay=startup_delay, quiet=quiet) or 0)
    monkeypatch.setattr(supervisor, "setup_logging", lambda logs: None)
    assert main.main(["--supervise", "--startup-delay", "15", "--quiet"]) == 0 and seen == {"delay": 15.0, "quiet": True}


# ---- autostart -----------------------------------------------------------------------------------------------------------------------
def test_the_registry_command_runs_the_supervisor_quietly_after_15_seconds():
    cmd = autostart.command()
    assert cmd.startswith('"') and "main.py" in cmd and "--supervise" in cmd and "--startup-delay 15" in cmd and cmd.endswith("--quiet")


def test_first_run_turns_it_on_once_and_says_so(_autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    assert autostart.ensure_default() == "enabled" and reg.value == autostart.command() and autostart.read_state() == "on"
    assert autostart.ensure_default() is None                  # the toast is shown only that one time


def test_off_removes_the_entry_completely_and_it_stays_off_forever(_autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    autostart.ensure_default()
    assert autostart.disable() is True and reg.value is None and autostart.read_state() == "off"
    for _ in range(3):                                          # restarts / updates
        assert autostart.ensure_default() is None and reg.value is None
    reg.value = "something else put it back"
    assert autostart.ensure_default() is None and reg.value is None      # even then the user's "off" wins
    autostart.enable()                                          # only an explicit --on / the tray toggle
    assert reg.value == autostart.command() and autostart.ensure_default() is None


def test_an_entry_removed_by_someone_else_is_respected_as_off(_autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    autostart.ensure_default()
    reg.value = None
    assert autostart.ensure_default() is None and reg.value is None and autostart.read_state() == "off"


def test_a_stale_path_is_quietly_corrected_while_it_is_on(_autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    autostart.ensure_default()
    reg.value = '"C:\\old\\pythonw.exe" "C:\\old\\main.py" --supervise'
    assert autostart.ensure_default() is None and reg.value == autostart.command()


def test_cli_on_off_status(capsys, _autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    assert autostart.main(["--status"]) == 0 and "OFF" in capsys.readouterr().out
    assert autostart.main(["--on"]) == 0 and reg.value
    assert autostart.main(["--status"]) == 0 and "ON" in capsys.readouterr().out
    assert autostart.main(["--off"]) == 0 and reg.value is None and "removed" in capsys.readouterr().out


def test_cli_off_reports_failure_if_the_entry_cannot_be_removed(capsys, _autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    reg.value = "x"
    reg.delete = lambda: None                                   # a registry that refuses
    assert autostart.main(["--off"]) == 1 and "Couldn't remove" in capsys.readouterr().out


# ---- orb watchdog --------------------------------------------------------------------------------------------------------------------
def make_orb():
    from ui.orb_frontend import OrbProcess

    class Proc:
        def __init__(self):
            self.rc = None

        def poll(self):
            return self.rc

        returncode = 1

    procs = []

    def popen(cmd, **kw):
        procs.append(Proc())
        return procs[-1]

    folder = Path(tempfile.mkdtemp())
    (folder / "vex_ui").mkdir()
    (folder / "vex_ui" / "app.py").write_text("")
    orb = OrbProcess(folder, port=1, click_port=2, popen=popen,                 # never the real orb's ports: stop() sends "quit"
                     port_in_use=lambda p: bool(procs and procs[-1].rc is None))
    return orb, procs


def test_a_crashed_orb_is_noticed_only_if_we_started_it():
    orb, procs = make_orb()
    assert orb.start(wait=1) and not orb.crashed()
    procs[-1].rc = 1
    assert orb.crashed()
    foreign, _ = make_orb()
    foreign.proc = SimpleNamespace(poll=lambda: 1)
    assert not foreign.crashed()                                # someone else's orb is none of our business


def test_the_orb_is_restarted_three_times_then_the_pill_takes_over():
    orb, procs = make_orb()
    gave_up = []
    assert orb.start(wait=1)
    w = ComponentWatch("the orb", lambda: not orb.crashed(), lambda: orb.start(wait=1), lambda n: gave_up.append(n), budget=RestartBudget(clock=Clock()))
    results = []
    for _ in range(5):
        procs[-1].rc = 1
        results.append(w.check())
    assert results == ["restarted"] * 3 + ["gave_up", "idle"] and len(procs) == 4 and gave_up == ["the orb"]


def test_a_clean_stop_of_the_orb_is_not_a_crash():
    orb, procs = make_orb()
    orb.start(wait=1)
    orb.stop()
    assert not orb.crashed()


def test_watch_thread_gives_up_into_the_callback():
    import time

    orb, procs = make_orb()
    orb.start(wait=1)
    got = []
    orb.watch(lambda: got.append("pill"), budget=RestartBudget(maximum=1, clock=Clock()), interval=0.01)
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        procs[-1].rc = 1
        time.sleep(0.03)
    orb._stopping = True
    assert got == ["pill"] and len(procs) == 2


# ---- tray: mute icon, autostart toggle, quiet start --------------------------------------------------------------------------------------
def icon_pixels(icon):
    img = icon.pixmap(64, 64).toImage()
    return {(x, y): img.pixelColor(x, y).name() for x in range(0, 64, 4) for y in range(0, 64, 4)}


def test_muted_and_listening_icons_are_clearly_different(qapp):
    from ui.tray import make_icon

    listening, muted = icon_pixels(make_icon(False)), icon_pixels(make_icon(True))
    assert sum(listening[k] != muted[k] for k in listening) > 40
    assert listening[(48, 48)] != muted[(48, 48)]               # the corner holds the green dot / red bar


@pytest.fixture
def tray(tmp_path, qapp, monkeypatch):
    from test_ui_qt import FakeController, FakeExecutor
    from ui.frontend import RecordingFrontend
    from ui.tray import TrayController

    cfg = tmp_path / "config.toml"
    cfg.write_text("[wakeword]\nenabled = true\n", encoding="utf-8")
    c = FakeController()
    c.state = "idle"
    c.on_mute_change = []
    t = TrayController(qapp, c, RecordingFrontend(), FakeExecutor(), config_path=cfg)
    shown = []
    monkeypatch.setattr(t.tray, "showMessage", lambda *a: shown.append(a[:2]))
    monkeypatch.setattr(t.tray, "isVisible", lambda: True)
    monkeypatch.setattr("ui.tray.QSystemTrayIcon.supportsMessages", staticmethod(lambda: True))
    t.shown = shown
    return t


def test_tray_mute_item_is_named_for_pausing_and_swaps_the_icon_and_status(tray):
    assert tray.actions["mute"].text() == "Mute mic / pause listening"
    tray.actions["mute"].trigger()
    assert tray.controller.mic_muted and "MUTED" in tray.status_action.text() and "muted" in tray.tray.toolTip()
    muted_px = icon_pixels(tray.tray.icon())
    tray.actions["mute"].trigger()
    assert "MUTED" not in tray.status_action.text() and icon_pixels(tray.tray.icon()) != muted_px


def test_the_icon_follows_a_mute_done_elsewhere(tray):
    tray.controller.mic_muted = True
    tray.refresh_mute_icon()
    assert tray.actions["mute"].isChecked() and "MUTED" in tray.status_action.text()


def test_start_with_windows_toggle_turns_autostart_on_and_off(tray, _autostart_never_touches_the_real_registry):
    reg = _autostart_never_touches_the_real_registry
    assert not tray.actions["autostart"].isChecked()
    tray.actions["autostart"].trigger()
    assert reg.value == autostart.command() and tray.actions["autostart"].isChecked()
    tray.actions["autostart"].trigger()
    assert reg.value is None and not tray.actions["autostart"].isChecked() and autostart.read_state() == "off"


def test_quiet_start_swallows_ordinary_toasts_but_not_forced_ones(tray):
    tray.set_quiet(60)
    tray.notify("Wake word", "listening")
    assert tray.shown == []
    tray.notify("Vex starts with Windows now", "once", force=True)
    assert len(tray.shown) == 1
    tray.quiet_until = 0
    tray.notify("Later", "normal")
    assert len(tray.shown) == 2


# ---- controller hook + watches --------------------------------------------------------------------------------------------------------
def test_muting_runs_the_mute_hooks_so_the_wake_listener_can_release_the_mic():
    from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
    from ui.controller import VexController
    from ui.frontend import RecordingFrontend

    c = VexController(FakeAgent(), RecordingFrontend(), FakeSpeaker(), FakeCapture("x"))
    seen = []
    c.on_mute_change.append(lambda: seen.append(c.mic_muted))
    c.set_mic_muted(True)
    c.set_mic_muted(False)
    assert seen == [True, False]


def test_build_watches_restart_the_hotkey_and_the_wake_word_but_not_while_muted():
    from ui.app import build_watches

    class Hotkey:
        active = False
        starts = 0

        def start(self):
            self.starts += 1

    class Wake:
        _thread = None
        starts = 0

        def start(self):
            self.starts += 1

    hk, wake = Hotkey(), Wake()
    tray = SimpleNamespace(actions={"wakeword": SimpleNamespace(isChecked=lambda: True)}, notify=lambda *a, **k: None)
    ctl = SimpleNamespace(mic_muted=False)
    hw, ww = build_watches(hk, True, wake, tray, ctl)
    assert (hw.check(), ww.check()) == ("restarted", "restarted") and (hk.starts, wake.starts) == (1, 1)
    ctl.mic_muted = True
    assert ww.check() == "idle"                                  # muted: the wake word is meant to be off
    never_registered, _ = build_watches(Hotkey(), False, wake, tray, ctl)
    assert never_registered.check() == "idle"                    # a hotkey that never registered (taken by another app) isn't "crashed"
