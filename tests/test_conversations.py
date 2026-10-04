"""Conversation history (ui/conversations.py): sessions, turns with their tools and timings, secrets redacted, retention."""
import json
import time
from datetime import datetime, timedelta

from test_followup import make

from ui.api_core import ApiCore
from ui.conversations import ConversationLog
from voice.timeline import Timeline


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 4, 18, 0, 0).astimezone()

    def __call__(self):
        return self.t


def test_a_session_collects_turns_tools_and_timings(tmp_path):
    log = ConversationLog(tmp_path / "c.jsonl", keep_days=14)
    log.start_session()
    log.add_tool({"event": "tool_start", "tool": "get_weather"})                    # starts are ignored
    log.add_tool({"event": "tool_end", "tool": "get_weather", "tier": "green", "decision": "auto", "ok": True})
    log.add_turn("weather in Paris?", "Paris is 18 degrees and sunny.")
    log.add_timing({"outcome": "spoken", "source": "wakeword", "stages": {"total": 3.1, "stt": 0.8, "brain": 1.2, "tts_first_audio": 0.6}})
    log.add_turn("and tomorrow?", "Rain tomorrow.")
    log.add_timing({"outcome": "no_speech", "stages": {}})                          # not a spoken turn: ignored
    s = log.end_session()
    assert len(s["turns"]) == 2 and s["turns"][0]["tools"][0]["tool"] == "get_weather" and s["turns"][1]["tools"] == []
    assert s["turns"][0]["latency"]["total"] == 3.1 and s["turns"][0]["via"] == "wakeword" and s["turns"][1]["latency"] is None
    row = log.list()[0]
    assert row["turns"] == 2 and row["first"] == "weather in Paris?" and row["tools"] == ["get_weather"] and row["median_total"] == 3.1
    assert json.loads((tmp_path / "c.jsonl").read_text(encoding="utf-8"))["id"] == s["id"]
    assert log.get(s["id"])["turns"][1]["reply"] == "Rain tomorrow."


def test_secrets_are_redacted_before_anything_is_stored(tmp_path):
    log = ConversationLog(tmp_path / "c.jsonl", keep_days=14)
    log.start_session()
    log.add_turn("my password is hunter2hunter2 and my card is 4111 1111 1111 1111", "I won't store that.")
    log.end_session()
    raw = (tmp_path / "c.jsonl").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw and "4111 1111 1111 1111" not in raw


def test_empty_sessions_are_not_kept(tmp_path):
    log = ConversationLog(tmp_path / "c.jsonl", keep_days=14)
    log.start_session()
    assert log.end_session() is None and log.list() == [] and not (tmp_path / "c.jsonl").exists()


def test_old_sessions_are_dropped_at_load_and_daily(tmp_path):
    c = Clock()
    p = tmp_path / "c.jsonl"
    log = ConversationLog(p, keep_days=14, clock=c)
    for days_ago in (20, 2):
        c.t = datetime(2026, 10, 4, 18, 0, 0).astimezone() - timedelta(days=days_ago)
        log.start_session()
        log.add_turn(f"said {days_ago} days ago", "ok")
        log.end_session()
    c.t = datetime(2026, 10, 4, 18, 0, 0).astimezone()
    again = ConversationLog(p, keep_days=14, clock=c)
    assert [r["first"] for r in again.list()] == ["said 2 days ago"]
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1                         # the file was trimmed too


def test_keep_zero_means_memory_only(tmp_path):
    log = ConversationLog(tmp_path / "c.jsonl", keep_days=0)
    log.start_session()
    log.add_turn("hi", "hello")
    log.end_session()
    assert log.list() and not (tmp_path / "c.jsonl").exists()


def test_the_api_core_hooks_it_to_real_turns(tmp_path, monkeypatch):
    tl = Timeline(time.monotonic)
    monkeypatch.setattr("voice.timeline.timeline", tl)
    monkeypatch.setattr("ui.controller.timeline", tl)
    c, cap, sp, fe, states, ends = make("what's the time", followups=["and the date?", None])
    log = ConversationLog(tmp_path / "c.jsonl", keep_days=14)
    core = ApiCore(controller=c, conversations=log, logs=tmp_path, data=tmp_path)
    core.attach()
    c.start_listening("wakeword")
    assert c.wait_idle(5)
    sessions = core.conversations_list({})
    assert len(sessions) == 1 and sessions[0]["turns"] == 2                           # the follow-up is in the same session
    full = core.conversations_get({"id": sessions[0]["id"]})
    assert [t["said"] for t in full["turns"]] == ["what's the time", "and the date?"]
    assert full["turns"][0]["latency"] is not None
