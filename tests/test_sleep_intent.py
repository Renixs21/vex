"""Sleep, robustly: a regex fast path for short clear commands, and the go_to_sleep tool for every other phrasing.
Both end the same way: a short reply, the orb parks as the pill, wake word only, never a follow-up window."""
import json

import pytest
from test_followup import make
from test_ui_core import FakeAgent

import tools
from brain.agent import Agent
from helpers import FakeLLM, ScriptedConfirmer
from memory import Memory
from safety.audit import AuditLog
from safety.executor import Executor
from tools import assistant, registry
from tools import memory as memory_tools
from ui.controller import done_intent

# ---- the fast path ---------------------------------------------------------------------------------------------------------------------
FAST = [
    "Okay Vex, you can go to sleep now.",          # vex.log 18:11:49 (wake phrase already stripped in the app; kept here on purpose)
    "you can go to sleep now.",                     # vex.log 18:11:49 after stripping
    "No, go to sleep.",                             # vex.log 18:12:10
    "Go to your doc.",                              # vex.log 18:12:02 (Whisper's "dock")
    "Stop, stop, stop, stop. Go to sleep.",         # vex.log 18:45:22
    "Go to sleep.",
    "go back to sleep",
    "Sleep.",
    "Go to your dock, Vex.",
    "go to the doc",
    "That's all.",
    "that's it",
    "Good night Vex.",
    "Goodnight.",
    "okay vex go to sleep",
    "Can you go to sleep?",                         # polite command, not a question about sleep
    "Goodbye.",
    "Stop listening.",
]


@pytest.mark.parametrize("said", FAST)
def test_short_clear_commands_take_the_fast_path(said):
    assert done_intent(said) == "sleep"


QUESTIONS = [
    "How do I go to sleep faster?",
    "What time should I sleep?",
    "Should I go to sleep now?",
    "Is it time to go to sleep?",
    "Why can't I sleep?",
    "Can I sleep for 20 minutes?",
    "Do you ever sleep?",
    "When should I go to bed?",
]


@pytest.mark.parametrize("q", QUESTIONS)
def test_questions_about_sleep_never_trigger_it(q):
    assert done_intent(q) is None


NOT_FAST = [
    "play sleep music",
    "set a reminder to go to sleep at 11",
    "go to sleep in ten minutes",
    "stop the music",
    "remind me that it's all done",
    "I'm heading to bed, you can rest too",          # a real sleep request, but not a short clear command: the brain handles it
]


@pytest.mark.parametrize("t", NOT_FAST)
def test_longer_or_other_requests_go_to_the_brain(t):
    assert done_intent(t) is None


def test_a_question_about_sleep_reaches_the_brain_and_keeps_the_window():
    c, cap, sp, fe, states, ends = make("How do I go to sleep faster?", followups=[None])
    c.start_listening("hotkey")
    assert c.wait_idle()
    assert c.agent.heard == ["How do I go to sleep faster?"] and len(cap.windows) == 1 and ("hide", None) not in fe.calls


# ---- the go_to_sleep tool ---------------------------------------------------------------------------------------------------------------
class SleepyAgent(FakeAgent):
    """Stands in for the brain deciding to call go_to_sleep (the real decision is the model's; the wiring is what's tested)."""

    def __init__(self, sleepy: set[str], reply: str = "Okay, sleeping."):
        super().__init__()
        self.sleepy, self.reply = sleepy, reply

    def run(self, text):
        self.heard.append(text)
        if text in self.sleepy:
            registry.execute("go_to_sleep", {})
            return self.reply
        return f"reply to {text}"


BRAIN_PHRASES = ["I'm heading to bed, you can rest too", "Okay we're finished here, take a break", "Alright, you can switch off now",
                 "Time for you to rest, buddy"]


@pytest.mark.parametrize("said", BRAIN_PHRASES)
def test_the_brain_tool_ends_the_turn_exactly_like_the_fast_path(said):
    tools.load_all()
    c, cap, sp, fe, states, ends = make(said, agent=SleepyAgent({said}))
    c.start_listening("wakeword")
    assert c.wait_idle()
    assert sp.spoken == ["Okay, sleeping."]                         # the model's short reply
    assert cap.windows == [] and ("hide", None) in fe.calls and c.state == "idle"


def test_a_long_reply_after_go_to_sleep_is_cut_to_okay():
    tools.load_all()
    c, cap, sp, *_ = make("we're done", agent=SleepyAgent({"we're done"}, reply="Sure thing! I'll head to sleep now. " * 3))
    c.start_listening("hotkey")
    c.wait_idle()
    assert sp.spoken == ["Okay."] and cap.windows == []


def test_the_sleep_flag_never_leaks_into_the_next_turn():
    tools.load_all()
    registry.execute("go_to_sleep", {})                            # e.g. a text-mode run with no controller to consume it
    c, cap, sp, fe, *_ = make("what's the time", followups=[None])
    c.start_listening("hotkey")
    c.wait_idle()
    assert sp.spoken == ["reply to what's the time"] and len(cap.windows) == 1 and ("hide", None) not in fe.calls


def test_go_to_sleep_is_green_core_and_always_offered():
    tools.load_all()
    t = registry.get("go_to_sleep")
    assert t.tier == "green" and t.category == "core" and t in registry.tools_for()
    assert registry.execute("go_to_sleep", {}).ok and assistant.take_sleep_request() and not assistant.take_sleep_request()


def test_through_the_real_agent_and_executor(tmp_path):
    """The model calls go_to_sleep -> the executor runs it without asking (green) -> audited -> the controller parks."""
    tools.load_all()
    mem = Memory(tmp_path / "vault")
    memory_tools.configure(mem)
    audit = AuditLog(tmp_path / "actions.jsonl")
    confirmer = ScriptedConfirmer()
    llm = FakeLLM([("go_to_sleep", {})], "Okay.")
    agent = Agent(llm=llm, executor=Executor(confirmer, audit, None), memory=mem)
    try:
        c, cap, sp, fe, *_ = make("alright, you can switch off now", agent=agent)
        c.start_listening("wakeword")
        assert c.wait_idle(5)
        assert sp.spoken == ["Okay."] and cap.windows == [] and ("hide", None) in fe.calls
        assert confirmer.requests == []                              # green: nobody was asked
        rows = [json.loads(line) for line in (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(r["tool"] == "go_to_sleep" for r in rows)
        assert "go_to_sleep" in llm.systems[0] and "go_to_sleep" in llm.tools_seen[0]
    finally:
        memory_tools.configure(None)
        mem.close()
