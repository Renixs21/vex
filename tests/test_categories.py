"""Tool groups: a small core set per request, the rest only when asked for or clearly needed."""
import json
import logging
from types import SimpleNamespace

import pytest

import tools
from brain.agent import Agent
from brain.llm import GroqClient
from helpers import YES, FakeLLM, ScriptedConfirmer
from memory import Memory
from safety.audit import AuditLog
from safety.executor import Executor
from tools import categories, registry

tools.load_all()
CORE = {"open_app", "play_youtube", "web_search", "recall", "remember", "more_tools"}


def approx_tokens(tool_list):
    return len(json.dumps([{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tool_list])) // 4


def test_every_tool_is_in_exactly_one_group_and_the_lists_match_the_registry():
    registered = {t.name for t in registry.all_tools()}
    grouped = [n for spec in categories.CATEGORIES.values() for n in spec["tools"]]
    assert len(grouped) == len(set(grouped)) and not set(grouped) & CORE
    assert set(grouped) | CORE == registered                          # nothing missing, nothing stale
    for t in registry.all_tools():
        expected = "core" if t.name in CORE else next(c for c, s in categories.CATEGORIES.items() if t.name in s["tools"])
        assert t.category == expected, t.name


def test_the_core_set_is_exactly_what_was_agreed():
    assert {t.name for t in registry.tools_for()} == CORE
    assert {t.name for t in registry.tools_for({"files"})} == CORE | set(categories.CATEGORIES["files"]["tools"])
    assert {t.name for t in registry.tools_for({"files", "shell"})} >= CORE | {"delete_file", "run_powershell"}


def test_the_core_payload_is_small():
    assert approx_tokens(registry.tools_for()) < 700
    assert approx_tokens(registry.all_tools()) > 2000                 # which is why not everything is sent


def test_every_description_is_one_short_line():
    for t in registry.all_tools():
        assert "\n" not in t.description and len(t.description) <= 90, (t.name, len(t.description))
        for name, prop in t.parameters["properties"].items():
            assert len(prop.get("description", "")) <= 70, (t.name, name)


@pytest.mark.parametrize("message,expected", [
    ("set the volume to 20 percent", {"system"}),
    ("set the brightness to 40", {"system"}),
    ("mute", {"system"}),
    ("create a file notes.txt on my desktop saying hi", {"files"}),
    ("delete the old screenshots from downloads", {"files", "system"}),
    ("install VLC", {"apps"}),
    ("close notepad", {"apps"}),
    ("pause the music", {"media"}),
    ("skip to the next track", {"media"}),
    ("remind me to call mum tomorrow", {"tasks"}),
    ("add buy milk to my to-do list", {"tasks"}),
    ("forget everything about Rahul", {"memory"}),
    ("run powershell: Get-Date", {"shell"}),
    ("shut down the pc", {"shell"}),
    ("cancel the shutdown", {"shell"}),
    ("what's the weather in Pune?", set()),
    ("open notepad", set()),
    ("play lofi beats on youtube", set()),
    ("remember that my roommate is Rahul", set()),
    ("who is my roommate", set()),
    ("hello there", set()),
    ("what is two plus two?", set()),
])
def test_keywords_pick_the_groups(message, expected):
    assert categories.match(message) == expected


def test_keywords_match_whole_words_only():
    assert categories.match("the mutest mutter") == set()               # not "mute"
    assert "files" in categories.match("FILES") and "system" in categories.match("Volume")


# ---- in the agent -----------------------------------------------------------------------------------------------------------------
@pytest.fixture
def make_agent(tmp_path):
    mem = Memory(tmp_path / "v")

    def make(llm, *answers):
        ex = Executor(ScriptedConfirmer(*answers), AuditLog(tmp_path / "a.jsonl"), None)
        return Agent(llm=llm, executor=ex, memory=mem), ex

    yield make
    mem.close()


def names(offered):
    return set(offered)


def test_the_first_request_carries_only_core_plus_matching_groups(make_agent):
    llm = FakeLLM("ok", "ok", "ok")
    agent, _ = make_agent(llm)
    agent.run("what is two plus two?")
    agent.run("set the volume to 20 percent")
    agent.run("create a file a.txt on my desktop")
    assert names(llm.tools_seen[0]) == CORE
    assert names(llm.tools_seen[1]) == CORE | set(categories.CATEGORIES["system"]["tools"])
    assert names(llm.tools_seen[2]) == CORE | set(categories.CATEGORIES["files"]["tools"])
    assert "run_powershell" not in llm.tools_seen[1] and "delete_file" not in llm.tools_seen[1]


def test_a_group_used_last_turn_stays_on_for_the_next_message(make_agent):
    llm = FakeLLM([("set_volume", {"level": 20})], "Volume set.", "Anything else?", "Paris.")
    agent, _ = make_agent(llm)
    import tools.system as system_tools

    system_tools._endpoint = lambda: SimpleNamespace(SetMasterVolumeLevelScalar=lambda v, c: None, SetMute=lambda m, c: None)
    agent.run("set the volume to 20 percent")
    agent.run("thanks")                                                  # no keyword, but the system group was just used
    assert "set_volume" in llm.tools_seen[2]
    agent.run("what is the capital of France?")                           # ...and drops off after one quiet turn
    assert "set_volume" not in llm.tools_seen[3]


def test_more_tools_adds_a_group_for_the_rest_of_the_turn(make_agent):
    llm = FakeLLM([("more_tools", {"category": "tasks"})], [("list_tasks", {})], "No open tasks.")
    agent, ex = make_agent(llm)
    agent.run("what's on my agenda")                                      # no keyword matched, so the model asks for the group
    assert "list_tasks" not in llm.tools_seen[0]
    assert "list_tasks" in llm.tools_seen[1] and "add_task" in llm.tools_seen[1]
    assert "Added the tasks tools" in llm.histories[1][-1]["result"]["message"]


def test_more_tools_rejects_unknown_groups(make_agent):
    llm = FakeLLM([("more_tools", {"category": "nuclear"})], "ok")
    agent, ex = make_agent(llm)
    agent.run("hello")
    assert llm.histories[1][-1]["result"]["ok"] is False
    assert "files" in llm.histories[1][-1]["result"]["message"]


def test_calls_to_tools_that_were_not_offered_are_refused_before_the_executor(make_agent):
    llm = FakeLLM([("delete_file", {"path": "C:\\x"})], "ok")
    agent, ex = make_agent(llm, YES)
    called = []
    ex.run = lambda name, args: called.append(name)
    agent.run("hello")
    assert called == [] and "isn't available right now" in llm.histories[1][-1]["result"]["message"]


def test_the_system_prompt_lists_the_groups(make_agent):
    llm = FakeLLM("ok")
    agent, _ = make_agent(llm)
    agent.run("hi")
    for name in categories.CATEGORIES:
        assert name in llm.systems[0]
    assert "more_tools" in llm.systems[0] and "{categories}" not in llm.systems[0]


# ---- token logging ----------------------------------------------------------------------------------------------------------------
def test_each_request_logs_provider_tools_and_tokens(caplog):
    g = GroqClient("openai/gpt-oss-120b", "k", 1000)

    def fake_create(**kw):
        msg = SimpleNamespace(content="Done.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=612, completion_tokens=9))

    g.client.chat.completions.create = fake_create
    with caplog.at_level(logging.INFO, logger="vex.llm"):
        g.generate("sys", [{"role": "user", "text": "hi"}], registry.tools_for())
    line = [r.getMessage() for r in caplog.records if "LLM request" in r.getMessage()][0]
    assert "provider=groq" in line and "tools=6" in line and "prompt_tokens=612" in line and "completion_tokens=9" in line


# ---- routing simple commands to the small model -------------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "set the volume to 20 percent", "mute", "unmute the sound", "set brightness to 40", "take a screenshot",
    "how much battery do I have left?", "open notepad", "play lofi beats on youtube", "play some jazz", "pause the music",
    "next track", "add buy milk to my tasks", "remind me in 20 minutes to stretch", "what's on my to-do list?", "close notepad",
    "lock my pc"])
def test_simple_commands_go_to_the_small_model(message):
    assert categories.simple_command(message)


@pytest.mark.parametrize("message", [
    "remember that my exam is on the 20th", "who is my roommate?", "what is two plus two?", "create a file notes.txt on my desktop saying hi",
    "run powershell: Get-Date", "search the web for python 3.13 release date", "delete the old screenshot", "forget Rahul",
    "summarise my notes", "shut down the pc", "set the volume and then open notepad and play jazz and mute and create a file",
    "set the volume to 20 and delete the file", "", "explain how the volume mixer works", "run it as administrator"])
def test_everything_else_stays_on_the_big_model(message):
    assert not categories.simple_command(message)


def test_the_agent_sets_the_routing_hint_per_message(make_agent, monkeypatch):
    llm = FakeLLM("ok", "ok", "ok")
    agent, _ = make_agent(llm)
    agent.run("set the volume to 20 percent")
    agent.run("remember that my exam is on the 20th")
    monkeypatch.setitem(__import__("config").settings["llm"], "route_simple", False)
    agent.run("mute")
    assert llm.prefer_seen == ["groq-small", None, None]


def test_remember_schema_never_offers_an_invalid_enum_default():
    t = registry.get("remember")
    assert t.parameters["required"] == ["fact"] and "default" not in t.parameters["properties"]["note_type"]
    for tool_ in registry.all_tools():                                  # general rule: an enum parameter may not default to a value outside it
        for name, prop in tool_.parameters["properties"].items():
            if "enum" in prop and "default" in prop:
                assert prop["default"] in prop["enum"], (tool_.name, name)
