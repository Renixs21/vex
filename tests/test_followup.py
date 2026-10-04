"""Follow-up listening: after a spoken reply Vex keeps listening for a few seconds without the wake word."""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
from test_ui_qt import qapp  # noqa: F401 - the shared QApplication fixture
from test_voice_recorder import AmplitudeVad, FakeStream

from ui.controller import FOLLOWUP, IDLE, LISTENING, VexController
from ui.frontend import RecordingFrontend
from voice.timeline import Timeline

BLOCK = object()


class FollowCapture(FakeCapture):
    """Normal turns answer from `texts`; follow-up windows from `followups` (a str, None = silence, BLOCK = wait until cancelled)."""

    def __init__(self, *texts, followups=(), **kw):
        super().__init__(*texts, **kw)
        self.followups = list(followups)
        self.windows: list[tuple[float, int, float]] = []          # (seconds, min_speech_ms, when)
        self.window_open = threading.Event()

    def listen_followup(self, seconds, min_speech_ms=250):
        self.windows.append((seconds, min_speech_ms, time.monotonic()))
        self.cancelled.clear()
        self.window_open.set()
        item = self.followups.pop(0) if self.followups else None
        if item is BLOCK:
            self.cancelled.wait(3)
            return None
        return item


class TimedSpeaker(FakeSpeaker):
    guard_s = 0.0

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ended: list[float] = []

    def speak(self, text, on_level):
        super().speak(text, on_level)
        self.ended.append(time.monotonic())


def make(*texts, followups=(), agent=None, **attrs):
    fe = RecordingFrontend()
    cap = FollowCapture(*texts, followups=followups)
    sp = TimedSpeaker()
    c = VexController(agent or FakeAgent(), fe, sp, cap)
    states, ends = [], []
    c.listeners.append(states.append)
    c.on_interaction_end.append(lambda: ends.append(1))             # in the app: the wake word listener's resume()
    for k, v in attrs.items():
        setattr(c, k, v)
    return c, cap, sp, fe, states, ends


# ---- step 1: the regression -------------------------------------------------------------------------------------------------------------
def test_after_a_reply_the_recorder_is_rearmed_and_then_the_wake_word():
    c, cap, sp, fe, states, ends = make("what's the time?")
    assert c.start_listening("wakeword") == "started" and c.wait_idle()
    assert sp.spoken == ["reply to what's the time?"]
    assert len(cap.windows) == 1                                   # the microphone listened again after the reply, no wake word
    assert states[-2:] == [FOLLOWUP, IDLE] and c.state == IDLE
    assert ends == [1]                                             # then (once) back to wake-word-only mode


def test_the_window_shows_as_listening_and_says_so():
    c, cap, sp, fe, states, ends = make("hi", followups=[BLOCK])
    c.start_listening("hotkey")
    assert cap.window_open.wait(2)
    caps = [v for op, v in fe.calls if op == "caption"]
    assert c.state == FOLLOWUP and "Still listening" in caps[-1]
    assert fe.states[-1] == LISTENING      # the orb's "listening": it won't autosleep
    c.cancel_capture()
    assert c.wait_idle()


# ---- step 2: the window ---------------------------------------------------------------------------------------------------------------
def test_silence_closes_the_window_quietly():
    c, cap, sp, fe, states, ends = make("open notepad", followups=[None])
    c.start_listening("hotkey")
    assert c.wait_idle()
    assert sp.spoken == ["reply to open notepad"] and c.state == IDLE    # nothing spoken when it expires
    assert ("caption", "") in fe.calls[-4:] and fe.states[-1] == "idle"


def test_speech_in_the_window_is_a_normal_turn_and_reopens_the_window():
    c, cap, sp, fe, states, ends = make("weather in Paris", followups=["and tomorrow?", "Hey Vex, what about Rome?", None])
    c.start_listening("wakeword")
    assert c.wait_idle(5)
    assert c.agent.heard == ["weather in Paris", "and tomorrow?", "what about Rome?"]
    assert len(cap.windows) == 3 and ends == [1]                  # one interaction from start to end, then the wake word again


def test_the_window_uses_the_configured_length_and_a_stricter_speech_start():
    c, cap, *_ = make("hi", followup_seconds=5.0, followup_min_speech_ms=300)
    c.start_listening("hotkey")
    c.wait_idle()
    assert cap.windows[0][:2] == (5.0, 300)


def test_the_window_opens_only_after_playback_has_ended_plus_the_echo_delay():
    c, cap, sp, *_ = make("hi")
    slept = []
    c._sleep = lambda s: (slept.append(s), time.sleep(s))
    c.start_listening("hotkey")
    c.wait_idle()
    assert slept == [pytest.approx(0.3)]
    assert cap.windows[0][2] - sp.ended[0] >= 0.29                 # Vex's own voice can't be what the window hears


def test_the_speakers_own_guard_counts_towards_the_delay():
    c, cap, sp, *_ = make("hi")
    sp.guard_s = 0.3                                               # the real speaker already waits 0.3 s after the last sound
    slept = []
    c._sleep = slept.append
    c.start_listening("hotkey")
    c.wait_idle()
    assert slept == [] and len(cap.windows) == 1


@pytest.mark.parametrize("phrase,short", [("That's all.", "Okay."), ("Stop listening.", "Okay."), ("Goodbye Vex.", "Okay."),
                                          ("No, go to sleep.", "Okay."), ("You can go to sleep now.", "Okay."), ("Go to your doc.", "Okay.")])
def test_ending_phrases_get_a_short_reply_close_the_window_and_park_the_orb(phrase, short):
    c, cap, sp, fe, states, ends = make("set a timer", followups=[phrase])
    c.start_listening("hotkey")
    assert c.wait_idle()
    assert c.agent.heard == ["set a timer"] and sp.spoken == ["reply to set a timer", short]      # no LLM call, just the short reply
    assert ("hide", None) in fe.calls and len(cap.windows) == 1 and c.state == IDLE


# ---- when the window must NOT open ------------------------------------------------------------------------------------------------------
def test_no_window_after_no_speech():
    c, cap, *_ = make()                                            # the capture hears nothing
    c.start_listening("hotkey")
    c.wait_idle()
    assert cap.windows == []


def test_no_window_after_an_error():
    c, cap, *_ = make("x", agent=FakeAgent(fail=True))
    c.start_listening("hotkey")
    c.wait_idle()
    assert cap.windows == []


def test_no_window_after_a_confirmation_question():
    class AskingAgent(FakeAgent):
        def run(self, text):
            ctl.note_confirmation()                                  # what ConfirmTracker does when a tool asks "Should I ...?"
            return super().run(text)

    ctl, cap, *_ = make("install vlc", agent=AskingAgent())
    ctl.start_listening("hotkey")
    ctl.wait_idle()
    assert cap.windows == []


def test_the_confirm_tracker_reports_every_question_and_passes_the_answer_through():
    from ui.app import ConfirmTracker

    asked = []
    inner = SimpleNamespace(confirm=lambda req: ("answer", req), dry_run=False)
    t = ConfirmTracker(inner, lambda: asked.append(1))
    assert t.confirm("q") == ("answer", "q") and asked == [1] and t.dry_run is False


def test_no_window_after_typed_text_a_reminder_sleep_or_when_off_or_muted():
    c, cap, *_ = make()
    c.submit_text("what's 2+2")
    c.wait_idle()
    c.announce("Reminder: stretch.")
    c.wait_idle()
    time.sleep(0.05)
    assert cap.windows == []
    for attrs in ({"followup_enabled": False}, {"followup_seconds": 0}):
        c2, cap2, *_ = make("hi", **attrs)
        c2.start_listening("hotkey")
        c2.wait_idle()
        assert cap2.windows == []
    c3, cap3, *_ = make("go to sleep")
    c3.start_listening("hotkey")
    c3.wait_idle()
    assert cap3.windows == []


def test_a_capture_without_follow_up_support_just_ends_the_turn():
    c = VexController(FakeAgent(), RecordingFrontend(), FakeSpeaker(), FakeCapture("hi"))
    c.start_listening("hotkey")
    assert c.wait_idle() and c.state == IDLE


# ---- interruptions ----------------------------------------------------------------------------------------------------------------------
def test_the_hotkey_during_the_window_starts_a_normal_turn():
    c, cap, sp, *_ = make("first", "second", followups=[BLOCK, None])
    c.start_listening("hotkey")
    assert cap.window_open.wait(2)
    cap.window_open.clear()
    assert c.start_listening("hotkey") == "barge_in"
    assert c.wait_idle(5)
    assert c.agent.heard == ["first", "second"] and cap.calls == 2   # the second one was a full normal capture


def test_typed_text_during_the_window_runs_as_the_next_turn():
    c, cap, *_ = make("first", followups=[BLOCK, None])
    c.start_listening("hotkey")
    assert cap.window_open.wait(2)
    assert c.submit_text("typed one") == "barge_in"
    assert c.wait_idle(5) and c.agent.heard == ["first", "typed one"]


def test_a_reminder_closes_the_window_and_is_spoken():
    c, cap, sp, *_ = make("first", followups=[BLOCK])
    c.start_listening("hotkey")
    assert cap.window_open.wait(2)
    assert c.announce("Reminder: call mom.") == "queued"
    assert c.wait_idle(3)
    deadline = time.time() + 5
    while "Reminder: call mom." not in sp.spoken and time.time() < deadline:
        time.sleep(0.05)
    assert "Reminder: call mom." in sp.spoken


def test_muting_closes_the_window():
    c, cap, *_ = make("first", followups=[BLOCK])
    c.start_listening("hotkey")
    assert cap.window_open.wait(2)
    c.set_mic_muted(True)
    assert c.wait_idle(3) and c.state == IDLE and len(cap.windows) == 1


# ---- the capture and recorder side --------------------------------------------------------------------------------------------------------
def test_listen_followup_passes_the_window_and_the_stricter_start_to_the_recorder():
    from voice.capture import VoiceCapture
    from voice.recorder import Recording

    seen = {}

    def record(cancel, no_speech_seconds=None, min_speech_ms=None):
        seen.update(no_speech=no_speech_seconds, min_speech=min_speech_ms)
        return Recording(np.zeros(0, np.int16), "no_speech", 8.0, 0.0, 5.0)

    stt = SimpleNamespace(available=True, transcribe=lambda a: "x")
    notices = []
    cap = VoiceCapture(SimpleNamespace(record=record, silence_ms=700, grace_ms=500), stt, notify=lambda *a: notices.append(a))
    assert cap.listen_followup(8, 250) is None and seen == {"no_speech": 8, "min_speech": 250}
    assert notices == []                                           # a quiet room after a reply is not "Vex can't hear you"


def test_listen_followup_is_quiet_when_the_mic_or_speech_recognition_is_missing():
    from voice.capture import VoiceCapture
    from voice.recorder import MicUnavailable

    def broken(cancel, **kw):
        raise MicUnavailable("unplugged")

    typed = []
    cap = VoiceCapture(SimpleNamespace(record=broken), SimpleNamespace(available=True), fallback=lambda: typed.append(1) or "typed")
    assert cap.listen_followup(8) is None and typed == []
    nokey = VoiceCapture(SimpleNamespace(record=broken), SimpleNamespace(available=False), fallback=lambda: typed.append(1) or "typed")
    assert nokey.listen_followup(8) is None and typed == []


def recorder_for(script):
    from voice.recorder import Recorder

    def factory(device, samplerate, channels, dtype, blocksize, callback):
        return FakeStream(script, samplerate, channels, blocksize, callback, period=0.003)

    return Recorder(silence_ms=300, stream_factory=factory, vad=AmplitudeVad(), no_speech_seconds=2)


def test_a_short_noise_does_not_open_a_follow_up_turn_but_does_count_normally():
    from voice.recorder import FRAME_MS

    burst = 200 // FRAME_MS                                        # ~200 ms of voiced audio: a cough, a door, the TTS tail
    script = [("silence", 5), ("speech", burst), ("silence", 120)]
    assert recorder_for(script).record(no_speech_seconds=1.5, min_speech_ms=250).reason == "no_speech"
    assert recorder_for(script).record(no_speech_seconds=1.5).reason == "speech"         # the normal 150 ms start would take it


# ---- logging, report, tray ------------------------------------------------------------------------------------------------------------------
def test_latency_rows_say_whether_the_turn_came_from_the_window(tmp_path, monkeypatch):
    t = Timeline(time.monotonic, tmp_path / "l.jsonl")
    monkeypatch.setattr("ui.controller.timeline", t)
    c, *_ = make("weather", followups=["and tomorrow?", None])
    c.start_listening("hotkey")
    assert c.wait_idle(5)
    rows = [json.loads(line) for line in (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["source"], r["followup"], r["outcome"]) for r in rows] == [
        ("hotkey", False, "spoken"), ("followup", True, "spoken"), ("followup", True, "followup_expired")]


def test_the_report_shows_follow_up_turns(capsys):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
    import latency_report

    base = {"endpoint_ms": 700, "stages": {"endpoint_wait": 0.7, "stt": 0.8, "brain": 1.0, "tts_first_audio": 0.4, "total": 2.9}}
    rows = [{**base, "ts": "2026-10-04T18:00:00", "source": "wakeword", "outcome": "spoken", "followup": False},
            {**base, "ts": "2026-10-04T18:00:09", "source": "followup", "outcome": "spoken", "followup": True},
            {"ts": "2026-10-04T18:00:20", "source": "followup", "outcome": "followup_expired", "followup": True, "stages": {}},
            {"ts": "2026-10-04T18:01:20", "source": "followup", "outcome": "followup_end", "followup": True, "stages": {}}]
    f = latency_report.summary(rows)["followup"]
    assert f == {"windows": 3, "used": 1, "expired": 1, "ended_by_phrase": 1}
    latency_report.print_report(rows)
    out = capsys.readouterr().out
    assert "followup" in out and "Follow-up windows: 3 opened, 1 used" in out


def test_tray_follow_up_toggle_switches_it_and_remembers(tmp_path, qapp, monkeypatch):  # noqa: F811
    import tomllib

    import config

    monkeypatch.setitem(config.settings["voice"], "followup_enabled", True)       # the toggle also updates the in-memory settings

    from test_ui_qt import FakeController, FakeExecutor
    from ui.tray import TrayController

    cfg = tmp_path / "config.toml"
    cfg.write_text("[voice]\nfollowup_enabled = true\n", encoding="utf-8")
    ctl = FakeController()
    ctl.followup_enabled = True
    tray = TrayController(qapp, ctl, RecordingFrontend(), FakeExecutor(), config_path=cfg)
    assert tray.actions["followup"].text() == "Follow-up listening" and tray.actions["followup"].isChecked()
    tray.actions["followup"].trigger()
    assert ctl.followup_enabled is False and tomllib.loads(cfg.read_text(encoding="utf-8"))["voice"]["followup_enabled"] is False


def test_followup_defaults_in_config():
    import config

    v = config.settings["voice"]
    assert v["followup_enabled"] is True and v["followup_s"] == 8 and v["followup_min_speech_ms"] == 250 and v["followup_delay_ms"] == 300
