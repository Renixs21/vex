import threading
import time

import numpy as np
import pytest

from safety.confirm import Confirmation, ConfirmRequest
from voice.capture import VoiceCapture
from voice.confirm import VoiceConfirmer, parse_yes_no, question_for
from voice.recorder import MicUnavailable, Recording
from voice.stt import SttLimitReached, SttUnavailable

AUDIO = (np.sin(np.arange(8000) / 9) * 5000).astype(np.int16)


class FakeRecorder:
    def __init__(self, *results, events=None):
        self.results, self.calls, self.events = list(results), [], events if events is not None else []

    def record(self, cancel=None, no_speech_seconds=None, max_seconds=None):
        self.calls.append({"no_speech": no_speech_seconds, "max": max_seconds})
        self.events.append("record")
        r = self.results.pop(0) if self.results else Recording(AUDIO, "speech", 1.0, 0.5, peak_rms=900.0)
        if isinstance(r, Exception):
            raise r
        if r == "wait":                                    # block until cancelled (like a real recording with silence)
            cancel.wait(5)
            return Recording(np.zeros(0, np.int16), "cancelled", 0.1)
        return r


class FakeStt:
    def __init__(self, *texts, available=True):
        self.texts, self.available, self.calls = list(texts), available, 0

    def transcribe(self, audio, rate=16000):
        self.calls += 1
        t = self.texts.pop(0) if self.texts else "open notepad"
        if isinstance(t, Exception):
            raise t
        return t


# ---- VoiceCapture ----------------------------------------------------------------------------------------------------------------
def test_capture_returns_the_transcript():
    cap = VoiceCapture(FakeRecorder(), FakeStt("Open Notepad."))
    assert cap() == "Open Notepad."


def test_nothing_said_or_cancelled_returns_none_without_calling_whisper():
    stt = FakeStt()
    for rec in (Recording(np.zeros(0, np.int16), "no_speech", 6.0), Recording(np.zeros(0, np.int16), "cancelled", 0.2)):
        assert VoiceCapture(FakeRecorder(rec), stt)() is None
    assert stt.calls == 0


def test_empty_transcript_is_none():
    assert VoiceCapture(FakeRecorder(), FakeStt(""))() is None


def test_a_missing_microphone_falls_back_to_typing_and_explains_once():
    notices, typed = [], []
    cap = VoiceCapture(FakeRecorder(MicUnavailable("blocked"), MicUnavailable("blocked")), FakeStt(),
                       fallback=lambda: typed.append(1) or "typed command", notify=lambda t, m: notices.append(m))
    assert cap() == "typed command" and cap() == "typed command"
    assert len(typed) == 2 and len(notices) == 1                                   # the notice isn't repeated every time
    assert "Privacy > Microphone" in notices[0]


def test_the_whisper_limit_falls_back_to_typing_with_the_time():
    notices = []
    cap = VoiceCapture(FakeRecorder(), FakeStt(SttLimitReached(1800)), fallback=lambda: "typed", notify=lambda t, m: notices.append(m))
    assert cap() == "typed" and "rate limited until about" in notices[0] and "Type your command" in notices[0]


def test_unreachable_speech_recognition_falls_back_to_typing():
    notices = []
    cap = VoiceCapture(FakeRecorder(), FakeStt(SttUnavailable("offline")), fallback=lambda: "typed", notify=lambda t, m: notices.append(m))
    assert cap() == "typed" and "couldn't reach speech recognition" in notices[0]


def test_no_groq_key_goes_straight_to_typing_without_recording():
    rec = FakeRecorder()
    cap = VoiceCapture(rec, FakeStt(available=False), fallback=lambda: "typed", notify=lambda t, m: None)
    assert cap() == "typed" and rec.calls == []


def test_without_a_fallback_a_failure_is_just_none():
    assert VoiceCapture(FakeRecorder(MicUnavailable("x")), FakeStt())() is None


def test_cancel_stops_the_recording_and_the_typed_prompt():
    cancelled = []

    class Typed:
        def __call__(self):
            return None

        def cancel(self):
            cancelled.append(1)

    cap = VoiceCapture(FakeRecorder("wait"), FakeStt(), fallback=Typed())
    out = {}
    t = threading.Thread(target=lambda: out.update(r=cap()))
    t.start()
    time.sleep(0.1)
    cap.cancel()
    t.join(3)
    assert out["r"] is None and cancelled == [1]


# ---- phrases ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["yes", "Yes.", "YEAH!", "yep", "Do it.", "go ahead", "Yes, go ahead.", "yes please", "Please do."])
def test_clear_yes_phrases(text):
    assert parse_yes_no(text) == "yes"


@pytest.mark.parametrize("text", ["no", "No.", "nope", "cancel", "Cancel that.", "stop", "Don't.", "do not", "never mind", "no thanks"])
def test_clear_no_phrases(text):
    assert parse_yes_no(text) == "no"


@pytest.mark.parametrize("text", ["", "maybe", "uh", "yes but not now", "yeah sure", "I guess so", "okay", "sure", "yes no", "yesterday",
                                  "do it later", "go", "not sure", "what", "stop the music and yes", "ya"])
def test_everything_else_is_unclear(text):
    assert parse_yes_no(text) == "unclear"


# ---- VoiceConfirmer --------------------------------------------------------------------------------------------------------------
def yellow(**kw):
    return ConfirmRequest(tool="lock_pc", tier="yellow", title="Should I do this?", detail="Lock the PC", timeout=kw.pop("timeout", 10), **kw)


def red(**kw):
    return ConfirmRequest(tool="delete_file", tier="red", title="Confirm", detail="Delete (to the Recycle Bin): C:\\x", requires_non_voice=True,
                          timeout=10, **kw)


class FakeDialog:
    def __init__(self, events):
        self.events, self.shown, self.resolved = events, threading.Event(), []
        self.answer, self._done = None, threading.Event()

    def confirm(self, req):
        self.events.append("dialog")
        self.shown.set()
        self._done.wait(8)
        return self.answer or Confirmation(False, "none", "timeout")

    def click(self, approved):
        self.answer = Confirmation(approved, "dialog", "" if approved else "declined")
        self._done.set()

    def resolve_active(self, result):
        self.resolved.append(result)
        self.answer = result
        self._done.set()


class FakeSpeaker:
    def __init__(self, events, hold=0.0):
        self.events, self.said, self.stops, self.hold = events, [], 0, hold
        self._stop = threading.Event()

    def say(self, text, on_level):
        self.said.append(text)
        self.events.append("say")
        self._stop.wait(self.hold)
        self.events.append("said")

    def stop(self):
        self.stops += 1
        self._stop.set()


def build(texts=("yes",), rec_results=(), hold=0.0, **kw):
    events = []
    dialog = FakeDialog(events)
    speaker = FakeSpeaker(events, hold)
    recorder = FakeRecorder(*rec_results, events=events)
    stt = FakeStt(*texts, **{k: v for k, v in kw.items() if k == "available"})
    vc = VoiceConfirmer(dialog, speaker, recorder, stt, window_seconds=6, **{k: v for k, v in kw.items() if k != "available"})
    return vc, dialog, speaker, recorder, stt, events


def test_a_spoken_yes_approves_yellow_on_the_voice_channel_and_closes_the_dialog():
    vc, dialog, speaker, recorder, stt, events = build(["Yes."])
    r = vc.confirm(yellow())
    assert r == Confirmation(True, "voice")
    assert speaker.said == ["Lock the PC. Should I go ahead?"]
    assert dialog.resolved and dialog.resolved[0].reason == "answered by voice"          # the open dialog was closed


def test_the_listening_window_opens_only_after_vex_finishes_asking_and_lasts_six_seconds():
    vc, dialog, speaker, recorder, stt, events = build(["yes"], hold=0.2)
    vc.confirm(yellow())
    assert events.index("said") < events.index("record")                                 # asked first, then listened
    assert recorder.calls == [{"no_speech": 6.0, "max": 10.0}]


def test_a_spoken_no_declines():
    vc, *_ = build(["No."])
    assert vc.confirm(yellow()) == Confirmation(False, "voice", "declined")


@pytest.mark.parametrize("heard", ["maybe", "yeah sure", "I guess so", "uh", "yes but not now"])
def test_anything_unclear_counts_as_no(heard):
    vc, *_ = build([heard])
    r = vc.confirm(yellow())
    assert r == Confirmation(False, "voice", "unclear")


def test_hearing_nothing_leaves_the_dialog_in_charge():
    vc, dialog, speaker, recorder, stt, events = build(rec_results=[Recording(np.zeros(0, np.int16), "no_speech", 6.0)])
    threading.Timer(0.4, dialog.click, args=(True,)).start()
    r = vc.confirm(yellow())
    assert r == Confirmation(True, "dialog") and stt.calls == 0


def test_a_click_during_the_question_wins_and_stops_the_voice():
    vc, dialog, speaker, recorder, stt, events = build(["yes"], hold=3.0)
    threading.Timer(0.2, dialog.click, args=(False,)).start()
    t = time.monotonic()
    r = vc.confirm(yellow())
    assert r == Confirmation(False, "dialog", "declined") and time.monotonic() - t < 2
    assert speaker.stops >= 1 and recorder.calls == [] and dialog.resolved == []


def test_a_click_while_listening_cancels_the_recording():
    vc, dialog, speaker, recorder, stt, events = build(rec_results=["wait"])
    threading.Timer(0.3, dialog.click, args=(True,)).start()
    t = time.monotonic()
    r = vc.confirm(yellow())
    assert r == Confirmation(True, "dialog") and time.monotonic() - t < 3 and stt.calls == 0


@pytest.mark.parametrize("req", [red(), ConfirmRequest(tool="t", tier="red", title="CONFIRM", detail="x", kind="typed", typed_word="CONFIRM",
                                                       requires_non_voice=True, timeout=10, step="catastrophic")])
def test_red_and_typed_confirmations_never_listen_and_say_to_use_the_screen(req):
    vc, dialog, speaker, recorder, stt, events = build(["yes"])
    threading.Timer(0.3, dialog.click, args=(True,)).start()
    r = vc.confirm(req)
    assert r == Confirmation(True, "dialog")
    assert recorder.calls == [] and stt.calls == 0                                       # a "yes" in the room can't approve it
    assert speaker.said == ["I need your approval on screen."]


@pytest.mark.parametrize("kwargs", [{"enabled": lambda: False}, {"is_muted": lambda: True}, {"available": False}])
def test_yellow_with_voice_off_or_muted_or_no_whisper_is_dialog_only(kwargs):
    vc, dialog, speaker, recorder, stt, events = build(["yes"], **kwargs)
    threading.Timer(0.3, dialog.click, args=(True,)).start()
    assert vc.confirm(yellow()) == Confirmation(True, "dialog")
    assert recorder.calls == [] and speaker.said == []


@pytest.mark.parametrize("failure", [MicUnavailable("busy"), SttLimitReached(60), SttUnavailable("offline")])
def test_voice_failures_fall_back_to_the_dialog_silently(failure):
    results = [failure] if isinstance(failure, MicUnavailable) else []
    texts = [failure] if not isinstance(failure, MicUnavailable) else []
    vc, dialog, speaker, recorder, stt, events = build(texts or ["yes"], rec_results=results)
    threading.Timer(0.4, dialog.click, args=(False,)).start()
    assert vc.confirm(yellow()) == Confirmation(False, "dialog", "declined")


def test_the_dialog_timeout_still_means_no():
    vc, dialog, speaker, recorder, stt, events = build(rec_results=[Recording(np.zeros(0, np.int16), "no_speech", 6.0)])
    dialog._done.set()                                   # the dialog times itself out immediately (answer None)
    assert vc.confirm(yellow(timeout=1)) == Confirmation(False, "none", "timeout")


def test_the_executor_would_still_reject_a_voice_answer_to_red():
    """Second lock: even if a voice answer slipped through for a red request, the executor refuses it."""
    from helpers import VOICE_YES, ScriptedConfirmer
    from safety.audit import AuditLog
    from safety.executor import Executor
    import tempfile
    from pathlib import Path
    import tools

    tools.load_all()
    tmp = Path(tempfile.mkdtemp())
    victim = tmp / "keep.txt"
    victim.write_text("x")
    from config import settings

    settings["safety"]["extra_safe_folders"] = [str(tmp)]
    try:
        r = Executor(ScriptedConfirmer(VOICE_YES), AuditLog(tmp / "a.jsonl"), None).run("delete_file", {"path": str(victim)})
        assert not r.ok and victim.exists()
    finally:
        settings["safety"]["extra_safe_folders"] = []


def test_the_spoken_question_uses_the_first_line_of_what_will_happen():
    req = ConfirmRequest(tool="install_app", tier="yellow", title="x", detail="Install app: VLC media player\n  ID: VideoLAN.VLC\n  Source: winget")
    assert question_for(req) == "Install app: VLC media player. Should I go ahead?"
    assert len(question_for(ConfirmRequest(tool="t", tier="yellow", title="x", detail="a" * 500))) < 170
