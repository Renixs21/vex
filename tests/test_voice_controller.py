import threading
import time

import pytest

from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
from ui.controller import IDLE, LISTENING, SPEAKING, THINKING, VexController, is_read_rest
from ui.frontend import RecordingFrontend


@pytest.mark.parametrize("text", [
    "read the rest", "Read the rest.", "say the rest", "speak the rest", "read the rest of it", "read it all", "read everything",
    "please read the rest", "read it out", "read it out loud", "read the whole thing", "continue reading", "say the rest of it",
    "Read me the rest.", "read it aloud", "read all of it"])
def test_read_the_rest_phrases(text):
    assert is_read_rest(text)


@pytest.mark.parametrize("text", [
    "read my notes", "read the file notes.txt", "what is the rest of the alphabet", "read", "say hello", "continue", "read it to me later",
    "open notepad", "", "read the rest of the files in downloads", "tell me the rest of the story about rahul"])
def test_other_requests_are_not_read_the_rest(text):
    assert not is_read_rest(text)


class RestSpeaker(FakeSpeaker):
    """A speaker with the VoiceSpeaker's speak_rest."""

    def __init__(self, rest="", hold=None):
        super().__init__(hold=hold)
        self.rest, self.rest_spoken = rest, []

    def speak_rest(self, on_level):
        self.speaking_rest = True
        if not self.rest:
            return False
        self.rest_spoken.append(self.rest)
        on_level(0.5)
        self.rest = ""
        return True


def make(speaker=None, capture=None, typed=None, agent=None):
    f = RecordingFrontend()
    c = VexController(agent or FakeAgent(), f, speaker or RestSpeaker(), capture or FakeCapture("hello"), typed_capture=typed)
    return c, f


def test_read_the_rest_is_spoken_without_asking_the_llm():
    sp = RestSpeaker(rest="The remaining sentences.")
    c, f = make(sp, FakeCapture("read the rest"))
    assert c.start_listening() == "started" and c.wait_idle()
    assert sp.rest_spoken == ["The remaining sentences."] and c.agent.heard == [] and sp.spoken == []
    assert f.states == [LISTENING, SPEAKING, IDLE]


def test_read_the_rest_with_nothing_left_says_so():
    sp = RestSpeaker(rest="")
    c, f = make(sp, FakeCapture("say the rest"))
    c.start_listening()
    assert c.wait_idle() and sp.spoken == ["There's nothing more to read."] and c.agent.heard == []
    assert ("caption", "There's nothing more to read.") in f.calls


def test_a_speaker_without_speak_rest_hands_the_phrase_to_the_agent():
    from ui.speaker import SimulatedSpeaker

    class Quick(SimulatedSpeaker):
        def speak(self, text, on_level):
            on_level(0.0)

    c, f = make(Quick(), FakeCapture("read the rest"))
    c.start_listening()
    assert c.wait_idle() and c.agent.heard == ["read the rest"]


def test_the_last_reply_is_remembered():
    c, f = make()
    c.start_listening()
    c.wait_idle()
    assert c.last_reply == "reply to hello"


# ---- typing ---------------------------------------------------------------------------------------------------------------------
def test_submit_text_skips_the_microphone():
    cap = FakeCapture("never used")
    c, f = make(capture=cap)
    assert c.submit_text("open notepad") == "started" and c.wait_idle()
    assert cap.calls == 0 and c.agent.heard == ["open notepad"]
    assert f.states == [THINKING, SPEAKING, IDLE]
    assert ("caption", "“open notepad”") in f.calls


def test_the_typed_prompt_is_used_for_type_a_command_even_when_the_mic_is_muted():
    mic, typed = FakeCapture("from the mic"), FakeCapture("typed words")
    c, f = make(capture=mic, typed=typed)
    c.set_mic_muted(True)
    assert c.start_listening("tray") == "muted"                       # voice is blocked...
    assert c.start_listening("tray", typed=True) == "started" and c.wait_idle()     # ...typing isn't
    assert typed.calls == 1 and mic.calls == 0 and c.agent.heard == ["typed words"]


def test_typed_hooks_and_busy_rules_match_listening():
    gate = threading.Event()
    typed = FakeCapture("hi", block=gate)
    c, f = make(typed=typed)
    starts = []
    c.on_interaction_start.append(lambda: starts.append(1))
    assert c.start_listening("tray", typed=True) == "started"
    time.sleep(0.05)
    assert c.start_listening("hotkey") == "busy" and c.submit_text("x") == "busy"
    gate.set()
    assert c.wait_idle() and starts == [1]


def test_text_submitted_while_speaking_stops_the_speech_and_runs_next():
    hold = threading.Event()
    sp = RestSpeaker(hold=hold)
    c, f = make(sp, FakeCapture("first"))
    c.start_listening()
    assert sp.started.wait(2)
    assert c.submit_text("second command") == "barge_in"
    time.sleep(0.2)
    hold.set()
    assert c.wait_idle(3)
    assert c.agent.heard == ["first", "second command"] and sp.stops >= 1


def test_type_a_command_while_speaking_opens_the_typed_prompt_next():
    hold = threading.Event()
    sp = RestSpeaker(hold=hold)
    mic, typed = FakeCapture("first"), FakeCapture("typed after")
    c, f = make(sp, mic, typed)
    c.start_listening()
    assert sp.started.wait(2)
    assert c.start_listening("tray", typed=True) == "barge_in"
    time.sleep(0.2)
    hold.set()
    assert c.wait_idle(3)
    assert typed.calls == 1 and c.agent.heard == ["first", "typed after"] and mic.calls == 1


def test_a_plain_barge_in_after_a_typed_request_still_listens():
    hold = threading.Event()
    sp = RestSpeaker(hold=hold)
    mic = FakeCapture("first", "second")
    c, f = make(sp, mic)
    c.start_listening()
    assert sp.started.wait(2)
    assert c.start_listening("hotkey") == "barge_in"
    time.sleep(0.2)
    hold.set()
    assert c.wait_idle(3) and mic.calls == 2


def test_submit_text_error_returns_to_idle():
    c, f = make(agent=FakeAgent(fail=True))
    c.submit_text("boom")
    assert c.wait_idle() and c.state == IDLE and ("caption", "Something went wrong. Try again.") in f.calls
