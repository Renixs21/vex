"""Brain deadline (a slow model is abandoned for the next one), the "One sec…" filler, confirmation time kept out of latency, and the
"Hey Vex" merge limit (wake-word variants stripped; a filler-only remainder is a greeting)."""
import logging
import threading
import time

import pytest
from test_followup import make
from test_sleep_and_greeting import wake_controller
from test_ui_core import FakeAgent

from brain.llm import FallbackClient, LLMResponse, ProviderTimeout
from voice.timeline import Timeline, summarize
from voice.wakeword import is_bare_wake, strip_wake_phrase


class Provider:
    def __init__(self, name, delay=0.0, fail=None, text="ok"):
        self.name, self.delay, self.fail, self.text, self.calls = name, delay, fail, text, 0

    def generate(self, system, messages, tools):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return LLMResponse(f"{self.text} from {self.name}", [])


MSG = [{"role": "user", "text": "hi"}]


# ---- the deadline ---------------------------------------------------------------------------------------------------------------------------
def test_a_slow_first_model_is_abandoned_for_the_next_one(caplog):
    slow, fast = Provider("groq", delay=1.0), Provider("groq-small")
    fb = FallbackClient([slow, fast], deadline=0.2)
    caplog.set_level(logging.INFO, logger="vex.llm")
    t0 = time.monotonic()
    r = fb.generate("s", MSG, [])
    assert r.text == "ok from groq-small" and time.monotonic() - t0 < 0.8
    assert fb.last_answer == ("groq-small", "groq timed out after 0.2 s")
    assert any("LLM answered by groq-small (groq timed out after 0.2 s)" in m for m in caplog.messages)
    assert any("no answer within 0.2 s" in m for m in caplog.messages)


def test_the_timed_out_model_is_not_tried_first_for_a_while():
    slow, fast = Provider("groq", delay=1.0), Provider("groq-small")
    fb = FallbackClient([slow, fast], deadline=0.2, timeout_cooldown=60)
    fb.generate("s", MSG, [])
    fb.generate("s", MSG, [])
    assert slow.calls == 1 and fast.calls == 2                    # the second request went straight to the fast one


def test_a_fast_first_model_answers_and_says_first_choice(caplog):
    caplog.set_level(logging.INFO, logger="vex.llm")
    fb = FallbackClient([Provider("groq"), Provider("gemini")], deadline=0.5)
    assert fb.generate("s", MSG, []).text == "ok from groq" and fb.last_answer == ("groq", "first choice")
    assert any("LLM answered by groq (first choice)" in m for m in caplog.messages)


def test_the_last_model_in_the_ladder_gets_all_the_time_it_needs():
    slow_last = Provider("gemini", delay=0.4)
    fb = FallbackClient([Provider("groq", fail=RuntimeError("boom")), slow_last], deadline=0.1)
    assert fb.generate("s", MSG, []).text == "ok from gemini"     # no one left to fall back to: no deadline


def test_a_failure_reason_is_reported_too():
    fb = FallbackClient([Provider("groq", fail=ConnectionError("x")), Provider("groq-small")], deadline=1.0)
    fb.generate("s", MSG, [])
    assert fb.last_answer == ("groq-small", "groq failed: ConnectionError")


def test_without_a_deadline_nothing_changes():
    slow = Provider("groq", delay=0.3)
    fb = FallbackClient([slow, Provider("groq-small")])
    assert fb.generate("s", MSG, []).text == "ok from groq"


def test_the_timeline_records_who_answered_and_why(monkeypatch):
    tl = Timeline(time.monotonic)
    monkeypatch.setattr("brain.llm.timeline", tl)
    tl.begin("hotkey")
    FallbackClient([Provider("groq", delay=1.0), Provider("groq-small")], deadline=0.1).generate("s", MSG, [])
    rec = tl.finish()
    assert rec["llm_answered_by"] == "groq-small" and "timed out" in rec["llm_why"]
    assert [c["ok"] for c in rec["llm"]] == [False, True]


def test_the_deadline_comes_from_config():
    import config
    from brain.llm import build_client

    assert config.settings["llm"]["first_response_deadline_s"] == 4.0
    try:
        assert build_client().deadline == 4.0
    except RuntimeError:
        pytest.skip("no LLM keys in this environment")


def test_provider_timeout_is_an_exception():
    assert issubclass(ProviderTimeout, Exception)


# ---- the filler ----------------------------------------------------------------------------------------------------------------------------
class SlowAgent(FakeAgent):
    def __init__(self, delay, confirm_ctl=None):
        super().__init__()
        self.delay, self.confirm_ctl = delay, confirm_ctl

    def run(self, text):
        if self.confirm_ctl is not None:
            self.confirm_ctl.note_confirmation()
        time.sleep(self.delay)
        return super().run(text)


def test_a_slow_brain_gets_one_sec_said_once():
    c, cap, sp, *_ = make("hard question", agent=SlowAgent(0.5), filler_after_s=0.15)
    c.start_listening("hotkey")
    assert c.wait_idle(5)
    assert sp.spoken == ["One sec…", "reply to hard question"]


def test_a_fast_brain_gets_no_filler():
    c, cap, sp, *_ = make("easy", agent=SlowAgent(0.0), filler_after_s=0.3)
    c.start_listening("hotkey")
    c.wait_idle(5)
    time.sleep(0.4)
    assert sp.spoken == ["reply to easy"]


def test_no_filler_once_a_confirmation_was_asked():
    c, cap, sp, *_ = make("delete it", filler_after_s=0.1)
    c.agent = SlowAgent(0.4, confirm_ctl=c)
    c.start_listening("hotkey")
    c.wait_idle(5)
    assert sp.spoken == ["reply to delete it"]


def test_filler_off_by_default_in_the_controller():
    c, cap, sp, *_ = make("hard question", agent=SlowAgent(0.3))
    c.start_listening("hotkey")
    c.wait_idle(5)
    assert sp.spoken == ["reply to hard question"]


def test_the_filler_is_marked_in_the_timeline(monkeypatch):
    tl = Timeline(time.monotonic)
    monkeypatch.setattr("ui.controller.timeline", tl)
    recs = []
    real_finish = tl.finish
    monkeypatch.setattr(tl, "finish", lambda outcome="spoken": recs.append(real_finish(outcome)) or recs[-1])
    c, *_ = make("hard question", agent=SlowAgent(0.4), filler_after_s=0.1)
    c.start_listening("hotkey")
    c.wait_idle(5)
    assert recs[0]["filler"] is True


def test_the_speaker_never_overlaps_two_voices():
    import numpy as np

    from voice.speaker import VoiceSpeaker
    from voice.tts import Audio

    active, peak = [0], [0]
    lock = threading.Lock()

    class Chain:
        last_provider = "edge"

        def synthesize(self, text):
            return Audio(np.zeros(240, np.float32), 24000)

    class Player:
        def play(self, samples, rate, on_level):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

        def stop(self):
            pass

    sp = VoiceSpeaker(Chain(), Player(), guard_s=0, sleep=lambda s: None)
    ts = [threading.Thread(target=sp.say, args=(t, lambda v: None)) for t in ("One sec.", "Should I delete it?", "Done.")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(3)
    assert peak[0] == 1


# ---- confirmation time is not brain latency ----------------------------------------------------------------------------------------------
def test_confirmation_wait_is_its_own_stage_and_leaves_brain_and_total():
    turn = {"source": "followup", "marks": {"trigger": 0.0, "speech_end": 1.0, "record_end": 1.7, "stt_start": 1.7, "stt_end": 3.7,
                                            "brain_end": 18.0, "first_audio": 18.7}, "llm": [], "tools": [], "data": {"confirm_wait": 11.7}}
    s = summarize(turn, "spoken")["stages"]
    assert s["confirm_wait"] == 11.7 and s["brain"] == pytest.approx(2.6) and s["total"] == pytest.approx(6.0) and s["total_wall"] == 17.7


def test_the_executor_times_the_confirmation(monkeypatch, tmp_path):
    import tools
    from helpers import YES, ScriptedConfirmer
    from safety.audit import AuditLog
    from safety.executor import Executor

    tools.load_all()
    tl = Timeline(time.monotonic)
    monkeypatch.setattr("safety.executor.timeline", tl)
    tl.begin("hotkey")

    class SlowYes(ScriptedConfirmer):
        def confirm(self, req):
            time.sleep(0.2)
            return super().confirm(req)

    from tools import registry
    from tools.registry import YELLOW, Tool, ToolResult

    ran = []
    harmless = Tool("test_yellow", "A harmless yellow test tool.", {"type": "object", "properties": {}, "required": []}, YELLOW,
                    lambda: ran.append(1) or ToolResult(True, "done"))
    monkeypatch.setitem(registry._TOOLS, "test_yellow", harmless)
    ex = Executor(SlowYes(YES), AuditLog(tmp_path / "a.jsonl"), None, dry_run=False)
    assert ex.run("test_yellow", {}).ok and ran == [1]
    assert tl._turn["data"]["confirm_wait"] >= 0.19


# ---- the "Hey Vex" merge limit ---------------------------------------------------------------------------------------------------------
BARE = ["Hey Vex.", "hey vix", "Hey vax.", "A Vex.", "Vex.", "Hey, Vex! Hey Vex.", "Hey Vics.", "Hey Vex, um.", "Hey Vex. Okay.",
        "Hey vex, are you there?", "X.", "Havix.", "Hey Wex, hello?"]


@pytest.mark.parametrize("t", BARE)
def test_whisper_style_bare_wakes_are_greetings(t):
    assert is_bare_wake(t), t


COMMANDS = [("Hey vax, open notepad", "open notepad"), ("hey vex hey vex what time is it", "what time is it"),
            ("a vex what is the time", "what is the time"), ("Hey Vex. Hey Vex, play jazz.", "play jazz."),
            ("Hey Vix, yes, open notepad", "yes, open notepad")]


@pytest.mark.parametrize("t,rest", COMMANDS)
def test_wake_variants_are_stripped_from_commands(t, rest):
    assert strip_wake_phrase(t) == rest and not is_bare_wake(t)


@pytest.mark.parametrize("t", ["Vexed by this traffic", "Okay.", "Next song", "Text Rahul", "open the vest pocket app"])
def test_no_false_wake_strip(t):
    assert strip_wake_phrase(t) == t.strip() and not is_bare_wake(t)


@pytest.mark.parametrize("heard", ["Hey Vex.", "hey vix, um", "Hey, Vex! Hey Vex.", "Hey Vex, are you there?"])
def test_a_merged_bare_wake_is_greeted_not_sent_to_the_brain(heard):
    c, cap, sp, fe, states = wake_controller(wakes=[heard], followups=["what's the time", None])
    c.start_listening("wakeword")
    assert c.wait_idle(5)
    assert sp.spoken[0] == "What can I do for you?" and c.agent.heard == ["what's the time"]


def test_a_bare_wake_heard_after_the_hotkey_is_greeted_too():
    c, cap, sp, *_ = make("Hey Vex, um.", followups=[None])
    c.start_listening("hotkey")
    c.wait_idle(5)
    assert sp.spoken == ["What can I do for you?"] and c.agent.heard == []
