import logging
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ui.hotkey import ERROR_HOTKEY_ALREADY_REGISTERED, GlobalHotkey, explain_error, parse_hotkey
from voice.recorder import NEAR_SILENT_RMS, Recording, rms_to_level


# ---- hotkey parsing / registration ---------------------------------------------------------------------------------------------------
def test_parse_hotkey():
    assert parse_hotkey("ctrl+alt+v") == (0x2 | 0x1, ord("V"))
    assert parse_hotkey("Win+Shift+F9") == (0x8 | 0x4, 0x78)
    for bad in ("v", "ctrl+", "hyper+v", "ctrl+nonsense"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


class FakeUser32:
    def __init__(self, ok=True):
        self.ok = ok
        self.quit = threading.Event()
        self.calls = []

    def RegisterHotKey(self, *a):
        self.calls.append(("register", a[1:]))
        return self.ok

    def UnregisterHotKey(self, *a):
        self.calls.append(("unregister",))

    def GetMessageW(self, ref, *a):
        self.quit.wait(2)
        return 0

    def PostThreadMessageW(self, *a):
        self.quit.set()


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 7


def test_registration_failure_is_logged_and_reported(caplog, monkeypatch):
    import ui.hotkey as hk

    monkeypatch.setattr(hk.ctypes, "get_last_error", lambda: ERROR_HOTKEY_ALREADY_REGISTERED)
    errors = []
    h = GlobalHotkey("ctrl+alt+v", lambda s: None, on_error=errors.append, user32=FakeUser32(ok=False), kernel32=FakeKernel32())
    with caplog.at_level(logging.INFO, logger="vex.hotkey"):
        assert h.start() is False
    assert not h.active and "already taken" in h.error
    assert errors and "Hotkey unavailable" in errors[0] and "tray" in errors[0]
    assert any("hotkey unavailable" in r.message for r in caplog.records)


def test_a_bad_combination_is_reported_not_raised():
    errors = []
    h = GlobalHotkey("nonsense", lambda s: None, on_error=errors.append, user32=FakeUser32(), kernel32=FakeKernel32())
    assert h.start() is False and errors and "modifier" in h.error


def test_successful_registration_and_stop(caplog):
    u = FakeUser32()
    h = GlobalHotkey("ctrl+alt+v", lambda s: None, user32=u, kernel32=FakeKernel32())
    with caplog.at_level(logging.INFO, logger="vex.hotkey"):
        assert h.start() and h.active
        h.stop()
    assert ("unregister",) in u.calls and not h.active
    assert any("registered" in r.message for r in caplog.records)


def test_explain_error_other_codes():
    assert "error 5" in explain_error(5, "ctrl+alt+v")


# ---- real Win32: the combination is really registered, a second registration is refused ----------------------------------------------
def test_real_registerhotkey_conflict_is_detected():
    combo = "ctrl+alt+shift+f23"
    a = GlobalHotkey(combo, lambda s: None)
    errors = []
    b = GlobalHotkey(combo, lambda s: None, on_error=errors.append)
    try:
        assert a.start()
        assert b.start() is False and "already taken" in b.error and errors
    finally:
        a.stop()
        b.stop()


# ---- levels / silence ----------------------------------------------------------------------------------------------------------------
def test_rms_to_level_is_visible_for_normal_speech():
    assert rms_to_level(0) == 0 and rms_to_level(300) > 0.25 and rms_to_level(10000) == 1.0


def test_near_silent_recording_is_logged_and_told_once(caplog, monkeypatch):
    import voice.capture
    from voice.capture import VoiceCapture

    monkeypatch.setattr(voice.capture, "default_mic_muted", lambda: False)

    rec = Recording(audio=np.zeros(16000, dtype=np.int16), seconds=1.0, speech_seconds=0.5, reason="speech", peak_rms=2.0)
    notes = []
    cap = VoiceCapture(SimpleNamespace(record=lambda cancel: rec), SimpleNamespace(available=True, transcribe=lambda audio: "hello"),
                       notify=lambda title, m: notes.append(m))
    with caplog.at_level(logging.INFO):
        assert cap() == "hello"
        cap()
    assert len(notes) == 1 and "almost silent" in notes[0]
    assert sum("mic is near-silent" in r.message for r in caplog.records) == 2
    assert any("speech recognition result: 'hello'" in r.message for r in caplog.records)


def test_a_muted_windows_microphone_is_named_as_the_cause(caplog, monkeypatch):
    import voice.capture
    from voice.capture import VoiceCapture

    monkeypatch.setattr(voice.capture, "default_mic_muted", lambda: True)
    rec = Recording(audio=np.zeros(16000, dtype=np.int16), seconds=1.0, speech_seconds=0.0, reason="no_speech", peak_rms=0.5)
    notes = []
    cap = VoiceCapture(SimpleNamespace(record=lambda cancel: rec), SimpleNamespace(available=True, transcribe=lambda a: "x"),
                       notify=lambda title, m: notes.append(m))
    with caplog.at_level(logging.WARNING):
        cap()
    assert len(notes) == 1 and "muted" in notes[0]
    assert any("WINDOWS HAS THE MICROPHONE MUTED" in r.message for r in caplog.records)


# ---- click-back logging --------------------------------------------------------------------------------------------------------------
def test_every_click_port_datagram_is_logged(caplog):
    from ui.orb_frontend import ClickListener

    got = []
    cl = ClickListener(0, got.append)
    with caplog.at_level(logging.INFO, logger="vex.orb"):
        cl.start()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for payload in (b"junk", b'{"state": "idle"}', b'{"click": true}'):
            s.sendto(payload, ("127.0.0.1", cl.port))
        end = time.time() + 2
        while not got and time.time() < end:
            time.sleep(0.02)
        time.sleep(0.1)
        cl.stop()
    msgs = [r.message for r in caplog.records if "click port" in r.message]
    assert len(msgs) == 3 and "not JSON" in msgs[0] and "not a click" in msgs[1] and "orb click" in msgs[2]


def test_first_level_per_turn_is_logged(caplog):
    from ui.orb_frontend import OrbFrontend

    o = OrbFrontend(port=1)
    with caplog.at_level(logging.INFO, logger="vex.orb"):
        o.set_state("listening")
        o.set_level(0.2)
        o.set_level(0.3)
        o.set_state("thinking")
        o.set_state("listening")
        o.set_level(0.4)
    assert sum("first level sent" in r.message for r in caplog.records) == 2


# ---- single instance -----------------------------------------------------------------------------------------------------------------
def test_second_instance_is_refused(tmp_path):
    from ui import single_instance as si

    name = f"Local\\VexTest{time.time_ns()}"
    pid = tmp_path / "vex.pid"
    assert si.acquire(name, pid) == (True, None)
    try:
        ok, other = si.acquire(name, pid)
        assert ok is False and other is not None
    finally:
        si.release()


def test_main_refuses_a_second_copy(monkeypatch, capsys):
    import main
    from ui import single_instance

    monkeypatch.setattr(single_instance, "acquire", lambda: (False, 4242))
    assert main.main([]) == 1
    assert "already running (process 4242)" in capsys.readouterr().out


def test_console_flag_parses():
    import main

    assert main.build_parser().parse_args(["--console"]).console is True
