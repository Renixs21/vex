"""Bug fixes after follow-up: sleep/goodbye intents never open a window and park the orb; a bare "Hey Vex" is answered out loud."""
import logging
import time
from types import SimpleNamespace

import numpy as np
import pytest
from test_followup import BLOCK, FollowCapture, TimedSpeaker, make
from test_ui_core import FakeAgent

from ui.controller import FOLLOWUP, IDLE, VexController, done_intent
from ui.frontend import RecordingFrontend


class WakeCapture(FollowCapture):
    """Answers after_wake() (the capture right after the wake word) from `wakes`."""

    def __init__(self, *texts, wakes=(), **kw):
        super().__init__(*texts, **kw)
        self.wakes = list(wakes)
        self.wake_waits = []

    def after_wake(self, seconds):
        self.wake_waits.append(seconds)
        return self.wakes.pop(0) if self.wakes else None


def wake_controller(wakes=(), followups=(), agent=None):
    fe = RecordingFrontend()
    cap = WakeCapture(wakes=wakes, followups=followups)
    sp = TimedSpeaker()
    c = VexController(agent or FakeAgent(), fe, sp, cap)
    states = []
    c.listeners.append(states.append)
    return c, cap, sp, fe, states


# ---- bug 1: sleep intents ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("said", ["go to sleep", "Okay Vex, you can go to sleep now.", "No, go to sleep.", "Go to your doc.", "goodbye",
                                  "bye bye", "good night", "that's all", "stop listening", "sleep"])
def test_a_sleep_intent_speaks_a_short_reply_opens_no_window_and_the_orb_sleeps(said):
    c, cap, sp, fe, states, ends = make(said)
    c.start_listening("hotkey")
    assert c.wait_idle()
    assert cap.windows == []                                       # never a follow-up window after these
    assert sp.spoken == ["Okay."] and c.agent.heard == []           # the short reply, no LLM call
    assert FOLLOWUP not in states and c.state == IDLE
    assert fe.calls[-1] == ("hide", None) or ("hide", None) in fe.calls[-3:]   # orb -> sleep (the pill)
    caps = [v for op, v in fe.calls if op == "caption"]
    assert "Still listening…" not in caps and caps[-1] == ""


def test_sleep_said_inside_a_follow_up_window_ends_it_too():
    c, cap, sp, fe, states, ends = make("what's the time", followups=["you can go to sleep now"])
    c.start_listening("wakeword")
    assert c.wait_idle()
    assert len(cap.windows) == 1 and sp.spoken == ["reply to what's the time", "Okay."] and c.agent.heard == ["what's the time"]
    assert states[-1] == IDLE and ("hide", None) in fe.calls and ends == [1]


def test_the_orb_frontend_turns_hide_into_its_sleep_state():
    from ui.orb_frontend import OrbFrontend

    sent = []
    o = OrbFrontend(port=1)
    o._send = lambda **m: sent.append(m)
    o.hide()
    assert sent == [{"state": "sleep"}]


def test_done_intents_are_not_confused_with_real_requests():
    for t in ["play sleep music", "stop the music", "go to sleep in ten minutes", "thanks for the weather, now play music",
              "what's the weather tomorrow?", "no", "okay"]:
        assert done_intent(t) is None, t
    for t in ["thank you so much", "thanks Vex", "nothing else", "never mind"]:      # not fast-path any more: the brain decides
        assert done_intent(t) is None, t


def test_after_sleep_the_heartbeat_shows_idle_not_followup():
    from ui.app import app_status

    c, *_ = make("go to sleep")
    c.start_listening("hotkey")
    c.wait_idle()
    tray = SimpleNamespace(actions={"wakeword": SimpleNamespace(isChecked=lambda: True)})
    s = app_status(c, SimpleNamespace(active=True), None, None, tray)
    assert s["state"] == "idle"


# ---- bug 2: the greeting after a bare "Hey Vex" -------------------------------------------------------------------------------------------
def test_a_bare_wake_word_is_greeted_out_loud_then_vex_listens_for_the_command():
    c, cap, sp, fe, states = wake_controller(wakes=["Hey Vex."], followups=["open notepad", None])
    assert c.start_listening("wakeword") == "started" and c.wait_idle(5)
    assert sp.spoken[0] == "What can I do for you?"                  # passed to TTS and played
    assert c.agent.heard == ["open notepad"]                         # the command came in the window after the greeting
    assert cap.wake_waits == [3.0]


def test_silence_after_the_wake_word_is_greeted_too():
    c, cap, sp, fe, states = wake_controller(wakes=[None], followups=[None])
    c.start_listening("wakeword")
    assert c.wait_idle(5)
    assert sp.spoken == ["What can I do for you?"] and len(cap.windows) == 1 and c.state == IDLE


def test_a_bare_hey_vex_inside_the_follow_up_window_is_greeted_too():
    c, cap, sp, fe, states, ends = make("what's the time", followups=["Hey Vex.", "and the date?", None])
    c.start_listening("hotkey")
    assert c.wait_idle(5)
    assert sp.spoken == ["reply to what's the time", "What can I do for you?", "reply to and the date?"]


def test_a_wake_with_a_command_is_not_greeted():
    c, cap, sp, fe, states = wake_controller(wakes=["Hey Vex, open notepad"])
    c.start_listening("wakeword")
    c.wait_idle(5)
    assert sp.spoken == ["reply to open notepad"]


def test_the_hotkey_with_silence_is_not_greeted():
    c, cap, sp, fe, states, ends = make()
    c.start_listening("hotkey")
    c.wait_idle()
    assert sp.spoken == []


def test_the_greeting_plays_during_and_after_the_quiet_start(qapp):  # noqa: F811
    """Quiet mode only holds back toasts: never a reply or the greeting."""
    from test_ui_qt import FakeExecutor
    from ui.tray import TrayController

    for quiet in (True, False):
        c, cap, sp, fe, states = wake_controller(wakes=[None], followups=[None])
        tray = TrayController(qapp, c, fe, FakeExecutor())
        if quiet:
            tray.set_quiet(90)
        c.start_listening("wakeword")
        assert c.wait_idle(5)
        assert sp.spoken == ["What can I do for you?"], quiet


def test_after_wake_uses_a_shorter_wait_for_the_command():
    from voice.capture import VoiceCapture
    from voice.recorder import Recording

    seen = {}

    def record(cancel, no_speech_seconds=None, **kw):
        seen["wait"] = no_speech_seconds
        return Recording(np.zeros(0, np.int16), "no_speech", 3.0, 0.0, 500.0)

    cap = VoiceCapture(SimpleNamespace(record=record, silence_ms=700, grace_ms=0), SimpleNamespace(available=True))
    assert cap.after_wake(3.0) is None and seen["wait"] == 3.0


def test_muted_wake_is_not_greeted():
    c, cap, sp, fe, states = wake_controller(wakes=[None])
    c.mic_muted = True
    assert c.start_listening("wakeword") == "muted"
    time.sleep(0.05)
    assert sp.spoken == []


# ---- every TTS explains itself in the log ----------------------------------------------------------------------------------------------------
class FakeChain:
    last_provider = "edge"

    def __init__(self, fail=False):
        self.fail = fail

    def synthesize(self, text):
        if self.fail:
            raise OSError("offline")
        from voice.tts import Audio

        return Audio(np.zeros(240, np.float32), 24000)


class FakePlayer:
    def play(self, samples, rate, on_level):
        pass

    def stop(self):
        pass


def test_tts_logs_played_skipped_and_cancelled(caplog):
    from voice.speaker import SpeechUnavailable, VoiceSpeaker

    caplog.set_level(logging.INFO, logger="vex.speaker")
    sp = VoiceSpeaker(FakeChain(), FakePlayer(), guard_s=0, sleep=lambda s: None)
    sp.speak("Hello there.", lambda v: None)
    assert any(r.message.startswith("tts played: 1 of 1") for r in caplog.records)
    sp.speak("", lambda v: None)
    assert any(r.message.startswith("tts skipped: nothing to say") for r in caplog.records)
    bad = VoiceSpeaker(FakeChain(fail=True), FakePlayer(), guard_s=0, sleep=lambda s: None)
    with pytest.raises(SpeechUnavailable):
        bad.speak("Hello.", lambda v: None)
    assert any(r.message.startswith("tts skipped: speech failed before any sound") for r in caplog.records)


def test_tts_logs_a_cancel(caplog):
    import threading

    from voice.speaker import VoiceSpeaker

    caplog.set_level(logging.INFO, logger="vex.speaker")
    gate = threading.Event()

    class SlowPlayer(FakePlayer):
        def play(self, samples, rate, on_level):
            gate.wait(2)

    sp = VoiceSpeaker(FakeChain(), SlowPlayer(), guard_s=0, sleep=lambda s: None)
    t = threading.Thread(target=sp.speak, args=("One. Two. Three.", lambda v: None))
    t.start()
    time.sleep(0.1)
    sp.stop()
    gate.set()
    t.join(3)
    assert any(r.message.startswith("tts cancelled") for r in caplog.records)


def test_the_controller_logs_why_a_reply_was_not_spoken(caplog):
    caplog.set_level(logging.INFO, logger="vex.controller")

    class EmptyAgent(FakeAgent):
        def run(self, text):
            return ""

    c, *_ = make("hi", agent=EmptyAgent())
    c.start_listening("hotkey")
    c.wait_idle()
    assert any("tts skipped: the reply was empty" in r.message for r in caplog.records)


def test_no_test_can_reach_the_real_orb():
    from ui.orb_frontend import OrbFrontend

    sent = []
    o = OrbFrontend()                                                # default port 47474: the conftest guard swallows it
    o._sock = SimpleNamespace(sendto=lambda data, addr: sent.append(addr))
    o.quit()
    assert sent == []


from test_ui_qt import qapp  # noqa: E402,F401


def test_a_short_phantom_thanks_from_whisper_is_dropped():
    from voice.stt import GroqWhisper

    assert GroqWhisper._clean({"text": "Thanks.", "segments": [{"no_speech_prob": 0.1}]}, 1.0) == ""
    assert GroqWhisper._clean({"text": "Thanks.", "segments": [{"no_speech_prob": 0.05}]}, 2.5) == "Thanks."     # a real, longer one stays


def test_leading_stops_before_a_sleep_intent_still_mean_sleep():
    # seen live: "Stop, stop, stop, stop. Go to sleep." went to the language model and opened a window
    assert done_intent("Stop, stop, stop, stop. Go to sleep.") == "sleep"
    assert done_intent("wait, no, that's all") == "sleep"
    assert done_intent("stop") is None and done_intent("stop the music") is None     # a bare "stop" is for media, not sleep
