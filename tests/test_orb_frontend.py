import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ROOT
from ui.frontend import RecordingFrontend
from ui.orb_frontend import ClickListener, MultiFrontend, OrbFrontend, OrbProcess, short_caption


class Receiver:
    """A UDP socket standing in for the orb."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(1.0)
        self.port = self.sock.getsockname()[1]

    def get(self) -> dict:
        return json.loads(self.sock.recvfrom(65536)[0].decode("utf-8"))

    def drain(self, wait=0.2) -> list[dict]:
        out = []
        self.sock.settimeout(wait)
        try:
            while True:
                out.append(json.loads(self.sock.recvfrom(65536)[0].decode("utf-8")))
        except socket.timeout:
            self.sock.settimeout(1.0)
            return out

    def close(self):
        self.sock.close()


@pytest.fixture
def rx():
    r = Receiver()
    yield r
    r.close()


# ---- OrbFrontend ---------------------------------------------------------------------------------------------------------------------
def test_states_levels_captions_and_show_hide_map_to_the_orb_protocol(rx):
    o = OrbFrontend(rx.port)
    o.show()
    o.set_state("listening")
    o.set_state("thinking")
    o.set_state("speaking")
    o.set_level(0.4)
    o.show_caption("Volume set to 20 percent.")
    o.set_state("idle")
    o.hide()
    assert rx.drain() == [{"wake": True, "state": "idle"}, {"state": "listening"}, {"state": "thinking"}, {"state": "speaking"},
                          {"level": 0.4}, {"caption": "Volume set to 20 percent."}, {"state": "idle"}, {"state": "sleep"}]


def test_levels_are_clamped_rounded_and_repeats_dropped(rx):
    o = OrbFrontend(rx.port)
    for v in (0.12345, 0.1234, 0.1234, 7, -3, float("nan"), "x", 0.0):
        o.set_level(v)
    assert [m["level"] for m in rx.drain()] == [0.123, 1.0, 0.0]


def test_a_state_change_resets_level_dedupe(rx):
    o = OrbFrontend(rx.port)
    o.set_level(0.5)
    o.set_state("speaking")
    o.set_level(0.5)
    assert [m for m in rx.drain() if "level" in m] == [{"level": 0.5}, {"level": 0.5}]


def test_unknown_states_are_ignored(rx):
    o = OrbFrontend(rx.port)
    o.set_state("sleep")                       # the orb has "sleep" but Vex's interface doesn't: use hide()
    o.set_state("dancing")
    assert rx.drain() == []


def test_unicode_captions_survive(rx):
    OrbFrontend(rx.port).show_caption("Rahul’s “roommate” — ಹೆಲ್ಲೋ")
    assert rx.get()["caption"] == "Rahul’s “roommate” — ಹೆಲ್ಲೋ"


def test_long_captions_are_shortened_at_a_word():
    text = "word " * 100
    out = short_caption(text.strip())
    assert len(out) <= 160 and out.endswith("…") and not out[:-1].endswith(" wor")
    assert short_caption("Short reply.") == "Short reply." and short_caption("  spaced   out  ") == "spaced out"
    assert len(short_caption("a" * 500)) == 160


def test_send_failures_never_raise(rx, monkeypatch):
    o = OrbFrontend(rx.port)
    o._sock = SimpleNamespace(sendto=lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))     # sockets are slotted: swap it whole
    o.set_state("speaking")
    o.set_level(0.3)
    o.show_caption("x")
    o.show()
    o.hide()


def test_sending_with_nobody_listening_is_harmless():
    o = OrbFrontend(port=1)                    # nothing listens there
    for _ in range(3):
        o.set_state("speaking")
        o.set_level(0.5)


def test_wire_format_matches_the_orbs_own_client(rx):
    """Same datagrams as vex_ui/client.py's VexUI for the calls both support, so protocol drift gets caught."""
    sys.path.insert(0, str(ROOT / "vex_ui"))
    try:
        from vex_ui.client import VexUI
    finally:
        sys.path.pop(0)
    ours, theirs = OrbFrontend(rx.port), VexUI(port=rx.port)
    ours.set_state("listening"); theirs.listening()
    ours.set_state("thinking"); theirs.thinking()
    ours.set_state("speaking"); theirs.speaking()
    ours.set_level(0.6); theirs.level(0.6)
    ours.show_caption("hi there"); theirs.caption("hi there")
    ours.hide(); theirs.sleep()
    ours.set_palette("violet"); theirs.palette("violet")
    got = rx.drain()
    assert len(got) == 14 and got[0::2] == got[1::2]                  # interleaved: ours, theirs, ours, theirs...
    ours.show(); theirs.wake(caption=None)
    a, b = rx.drain()
    assert a == b == {"wake": True, "state": "idle"}


def test_the_orbs_server_accepts_every_message_we_send():
    """Run the orb's real tick() message handling against our datagrams, without a GPU or a window."""
    sys.path.insert(0, str(ROOT / "vex_ui"))
    try:
        pytest.importorskip("moderngl")
        from vex_ui.app import ControlServer
    finally:
        sys.path.pop(0)
    srv = ControlServer(0)
    port = srv.sock.getsockname()[1]
    srv.start()
    o = OrbFrontend(port)
    o.show(); o.set_state("speaking"); o.set_level(0.7); o.show_caption("Hello"); o.hide()
    time.sleep(0.3)
    msgs = srv.drain()
    assert {"wake": True, "state": "idle"} in msgs and {"level": 0.7} in msgs and {"caption": "Hello"} in msgs and {"state": "sleep"} in msgs
    from vex_ui.renderer import STATES

    assert all(m["state"] in STATES for m in msgs if "state" in m)


# ---- MultiFrontend ---------------------------------------------------------------------------------------------------------------------
def test_multi_frontend_fans_out_and_isolates_failures():
    a, b = RecordingFrontend(), RecordingFrontend()

    class Broken:
        def __getattr__(self, name):
            def boom(*args):
                raise RuntimeError("frontend crashed")
            return boom

    m = MultiFrontend([a, Broken(), b])
    m.show(); m.set_state("speaking"); m.set_level(2); m.show_caption("x"); m.hide()
    expected = [("show", None), ("state", "speaking"), ("level", 1.0), ("caption", "x"), ("hide", None)]
    assert a.calls == expected and b.calls == expected


# ---- OrbProcess -------------------------------------------------------------------------------------------------------------------------
class FakePopen:
    def __init__(self, cmd, **kw):
        self.cmd, self.kw, self.returncode = cmd, kw, None
        self.waited = self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def terminate(self):
        self.terminated = True


def orb_folder(tmp_path, with_app=True):
    (tmp_path / "vex_ui").mkdir(parents=True, exist_ok=True)
    if with_app:
        (tmp_path / "vex_ui" / "app.py").write_text("# orb")
    return tmp_path


def test_command_line_has_the_configured_options(tmp_path):
    p = OrbProcess(orb_folder(tmp_path), port=1234, click_port=1235, mode="overlay", position="bottom-right", palette="violet", size="420x340",
                   autosleep=20)
    cmd = p.command()
    assert cmd[1:4] == ["-m", "vex_ui.app", "--mode"] and cmd[0].lower().endswith(("pythonw.exe", "python.exe"))
    joined = " ".join(cmd)
    for part in ("--position bottom-right", "--palette violet", "--port 1234", "--click-port 1235", "--size 420x340", "--autosleep 20"):
        assert part in joined
    assert "--size" not in OrbProcess(orb_folder(tmp_path)).command() and "--autosleep" not in OrbProcess(tmp_path).command()


def test_dock_mode_is_the_default_and_uses_the_notify_port(tmp_path):
    cmd = " ".join(OrbProcess(orb_folder(tmp_path), port=1234, click_port=1235, edge="left", edge_y=0.3, autosleep=20).command())
    for part in ("--mode dock", "--port 1234", "--notify-port 1235", "--edge left", "--edge-y 0.3", "--autosleep 20"):
        assert part in cmd
    assert "--click-port" not in cmd and "--position" not in cmd


def test_an_orb_that_is_already_running_is_left_alone(tmp_path):
    launched = []
    p = OrbProcess(orb_folder(tmp_path), popen=lambda *a, **k: launched.append(1), port_in_use=lambda port: True)
    assert p.start() and launched == [] and not p.started_by_us
    p.stop()                                                          # and never stopped by us


def test_start_launches_in_the_orb_folder_and_waits_for_the_port(tmp_path):
    state = {"calls": 0}

    def in_use(port):
        state["calls"] += 1
        return state["calls"] > 3                                     # free at first, then the orb binds it

    made = []
    p = OrbProcess(orb_folder(tmp_path), popen=lambda cmd, **kw: made.append(FakePopen(cmd, **kw)) or made[-1], port_in_use=in_use)
    assert p.start(wait=3) and p.started_by_us
    assert made[0].kw["cwd"] == str(tmp_path) and made[0].cmd == p.command()


def test_missing_orb_folder_or_launch_failure_returns_false(tmp_path):
    assert not OrbProcess(orb_folder(tmp_path / "a", with_app=False), port_in_use=lambda p: False).start()

    def fail(*a, **k):
        raise OSError("no pythonw")

    assert not OrbProcess(orb_folder(tmp_path / "b"), popen=fail, port_in_use=lambda p: False).start()


def test_an_orb_that_dies_immediately_returns_false(tmp_path):
    def crash(cmd, **kw):
        fp = FakePopen(cmd, **kw)
        fp.returncode = 1
        return fp

    assert not OrbProcess(orb_folder(tmp_path), popen=crash, port_in_use=lambda p: False).start(wait=2)


def test_an_orb_that_never_listens_times_out(tmp_path):
    t = time.monotonic()
    assert not OrbProcess(orb_folder(tmp_path), popen=lambda c, **k: FakePopen(c, **k), port_in_use=lambda p: False).start(wait=0.4)
    assert time.monotonic() - t < 2


def test_stop_asks_the_orb_to_quit_only_if_we_started_it(tmp_path, rx):
    made = []
    p = OrbProcess(orb_folder(tmp_path), port=rx.port, popen=lambda c, **k: made.append(FakePopen(c, **k)) or made[-1],
                   port_in_use=lambda port: len(made) > 0)
    assert p.start(wait=2)
    p.stop()
    assert rx.get() == {"quit": True} and made[0].waited and p.proc is None


def test_a_stuck_orb_is_terminated(tmp_path, rx):
    made = []

    class Stuck(FakePopen):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("orb", timeout)

    p = OrbProcess(orb_folder(tmp_path), port=rx.port, popen=lambda c, **k: made.append(Stuck(c, **k)) or made[-1],
                   port_in_use=lambda port: len(made) > 0)
    p.start(wait=2)
    p.stop()
    assert made[0].terminated


# ---- clicks from the orb -------------------------------------------------------------------------------------------------------------------
def test_a_click_from_the_orb_starts_listening():
    got = []
    cl = ClickListener(0, got.append)
    cl.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b'{"click": true}', ("127.0.0.1", cl.port))
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.02)
        assert got == ["orb-click"]
    finally:
        cl.stop()


def test_a_click_on_the_dock_pill_starts_listening():
    got = []
    cl = ClickListener(0, got.append)
    cl.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b'{"event": "wake_clicked"}', ("127.0.0.1", cl.port))
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.02)
        assert got == ["orb-click"]
    finally:
        cl.stop()


def test_only_real_click_messages_count_and_a_failing_handler_does_not_kill_the_listener():
    calls = []

    def handler(src):
        calls.append(src)
        if len(calls) == 1:
            raise RuntimeError("boom")

    cl = ClickListener(0, handler)
    cl.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for payload in (b"garbage", b"\xff\xfe", b'{"click": false}', b'{"state": "idle"}', b'[1,2]', b'{"click": "yes"}',
                        b'{"click": true}', b'{"click": true}'):
            s.sendto(payload, ("127.0.0.1", cl.port))
        deadline = time.time() + 2
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert calls == ["orb-click", "orb-click"]
    finally:
        cl.stop()


# ---- choosing the visual ------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def fake_orb(monkeypatch):
    import ui.orb_frontend as of

    state = {"start_ok": True, "created": []}

    class FakeOrbProcess:
        def __init__(self, folder, **kw):
            self.folder, self.kw, self.stopped = folder, kw, False
            state["created"].append(self)

        def start(self, wait=10.0):
            return state["start_ok"]

        def stop(self):
            self.stopped = True

        def watch(self, on_giveup, **kw):
            self.on_giveup = on_giveup

    monkeypatch.setattr(of, "OrbProcess", FakeOrbProcess)
    monkeypatch.setattr(of, "ClickListener", lambda port, cb: SimpleNamespace(start=lambda: None, stop=lambda: None, port=port))
    return state


def ui_cfg(**kw):
    base = {"visual": "orb", "orb_dir": "vex_ui", "orb_position": "bottom", "orb_palette": "blue", "orb_size": "", "orb_autosleep": 0, "orb_mode": "dock", "orb_edge": "right", "orb_edge_y": 0.4, "orb_center_y": 0.5, "orb_radius": 62.0,
            "orb_port": 47474, "orb_click_port": 47475}
    return {**base, **kw}


def test_visual_orb_uses_the_orb_and_keeps_the_pill_hidden(fake_orb):
    from ui.app import build_visual

    pill = RecordingFrontend()
    front, orb, clicks = build_visual(ui_cfg(), pill, lambda s: None, lambda t, m: None)
    assert isinstance(front, MultiFrontend) and orb is not None and clicks is not None
    assert pill.calls == [("hide", None)]
    assert fake_orb["created"][0].kw["palette"] == "blue" and fake_orb["created"][0].kw["click_port"] == 47475


def test_visual_both_drives_the_orb_and_the_pill(fake_orb):
    from ui.app import build_visual

    pill = RecordingFrontend()
    front, orb, _ = build_visual(ui_cfg(visual="both"), pill, lambda s: None, lambda t, m: None)
    front.set_state("speaking")
    assert ("state", "speaking") in pill.calls and ("hide", None) not in pill.calls


def test_visual_pill_never_starts_the_orb(fake_orb):
    from ui.app import build_visual

    pill = RecordingFrontend()
    front, orb, clicks = build_visual(ui_cfg(visual="pill"), pill, lambda s: None, lambda t, m: None)
    assert front is pill and orb is None and clicks is None and fake_orb["created"] == []


def test_if_the_orb_cant_start_the_pill_is_used_and_you_are_told(fake_orb):
    from ui.app import build_visual

    fake_orb["start_ok"] = False
    notes = []
    pill = RecordingFrontend()
    front, orb, clicks = build_visual(ui_cfg(), pill, lambda s: None, lambda t, m: notes.append(m))
    assert front is pill and orb is None and clicks is None and ("hide", None) not in pill.calls
    assert notes and "pill" in notes[0]


def test_the_default_config_selects_the_orb():
    import config

    assert config.settings["ui"]["visual"] == "orb" and config.settings["ui"]["orb_dir"] == "vex_ui"
    assert (ROOT / config.settings["ui"]["orb_dir"] / "vex_ui" / "app.py").is_file()
