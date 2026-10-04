import json
import shutil
from types import SimpleNamespace

import pytest

from memory import forget as forget_mod
from memory.curator import (Curator, Decision, SYSTEM_PROMPT, decide, is_personal, parse_reply, transcript, worth_curating, Turn)
from memory.secrets import find_secrets


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, system, messages, tools):
        self.calls.append((system, messages, tools))
        out = self.replies.pop(0) if self.replies else '{"summary": "", "facts": []}'
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out if isinstance(out, str) else json.dumps(out), tool_calls=[], raw=None, usage=None)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def fact(note="Rahul", fact="Roommate since 2024", ntype="person", conf=0.9, action="add", **kw):
    return {"action": action, "note": note, "note_type": ntype, "fact": fact, "confidence": conf, **kw}


@pytest.fixture
def cur(mem, tmp_path):
    clock = Clock()

    def make(llm, **kw):
        return Curator(mem, llm, idle_seconds=120, min_words=6, queue_path=tmp_path / "queue.json", log_path=tmp_path / "curator.jsonl",
                       clock=clock, **kw)

    make.clock = clock
    make.log = tmp_path / "curator.jsonl"
    return make


def say(c, user="My roommate Rahul is helping me with my portfolio site this month", reply="Nice."):
    c.add_turn(user, reply)


def finish(c, clock):
    clock.t += 130
    return c.run_pending()


def audit(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---- policy (independent of the LLM) --------------------------------------------------------------------------------------
def D(**kw):
    base = dict(action="add", note="Portfolio Site", note_type="project", fact="Deadline is the 20th", confidence=0.9)
    return Decision(**{**base, **kw})


def test_project_facts_need_0_7_and_go_straight_to_notes():
    assert decide(D(confidence=0.75)) == ("write", "ok")
    assert decide(D(confidence=0.69))[0] == "inbox"


def test_personal_facts_need_0_8_else_inbox():
    for kw in ({"note": "me", "note_type": "profile"}, {"note": "Preferences", "note_type": "preference"},
               {"note": "Rahul", "note_type": "person"}, {"note": "Home", "note_type": "place"}):
        assert is_personal(D(**kw))
        assert decide(D(confidence=0.79, **kw))[0] == "inbox"
        assert decide(D(confidence=0.8, fact="Likes tea", **kw))[0] == "write"


def test_secrets_are_never_written_even_at_full_confidence():
    for text in ("My password is hunter22secret", "Card number 4111 1111 1111 1111", "API key is sk-abcdefghijklmnopqrstuvwx",
                 "Aadhaar 1234 5678 9012", "PAN ABCDE1234F"):
        assert find_secrets(text), text
        assert decide(D(fact=text, confidence=1.0))[0] == "skip"


def test_sensitive_facts_about_other_people_go_to_the_inbox():
    d = D(note="Rahul", note_type="person", fact="Rahul was diagnosed with diabetes", confidence=0.95)
    assert decide(d)[0] == "inbox"
    assert decide(D(note="Priya", note_type="person", fact="Priya owes a big loan to her uncle", confidence=0.95))[0] == "inbox"
    # about the user themselves it is allowed (still subject to the personal threshold)
    assert decide(D(note="me", note_type="profile", fact="Takes medication in the morning", confidence=0.9))[0] == "write"


def test_deletions_are_only_proposed():
    assert decide(D(action="delete", replaces_block="f1"))[0] == "inbox"


def test_prompt_lists_the_never_save_categories():
    for word in ("passwords", "API keys", "card or bank account numbers", "government ID", "OTHER people's health, money or private life",
                 "When unsure"):
        assert word in SYSTEM_PROMPT


# ---- parsing --------------------------------------------------------------------------------------------------------------
def test_parse_reply_tolerates_code_fences_and_bad_items():
    text = '```json\n{"summary": "Talked about Rahul.", "facts": [{"action": "add", "note": "Rahul", "note_type": "person", "fact": "x", "confidence": "0.8", "replaces_block": "^f3"}, "junk", {"action": "explode"}]}\n```'
    summary, ds = parse_reply(text)
    assert summary == "Talked about Rahul." and len(ds) == 1 and ds[0].confidence == 0.8 and ds[0].replaces_block == "f3"
    with pytest.raises(ValueError):
        parse_reply("no json here")


def test_trivial_conversations_are_skipped_and_transcript_redacts_secrets():
    assert not worth_curating([Turn("open notepad", "Done."), Turn("play lofi", "Playing.")], 6)
    assert worth_curating([Turn("my roommate Rahul helps with the site", "ok")], 6)
    assert "hunter22" not in transcript([Turn("my password is hunter22secret ok", "no")])


# ---- end to end -----------------------------------------------------------------------------------------------------------
def test_saves_a_fact_with_provenance_and_a_daily_summary(cur, mem):
    llm = FakeLLM({"summary": "The user talked about Rahul and the portfolio site.",
                   "facts": [fact(), fact("Portfolio Site", "Built with Next.js", "project", 0.9)]})
    c = cur(llm)
    say(c)
    assert finish(c, cur.clock) == 1 and c.pending() == 0
    assert "Roommate since 2024" in mem.recall("who is Rahul").message
    assert "Next.js" in mem.recall("portfolio site").message
    assert "daily/" in (mem.vault.root / "people" / "rahul.md").read_text(encoding="utf-8")
    daily = list((mem.vault.root / "daily").glob("*.md"))
    assert daily and "talked about Rahul" in daily[0].read_text(encoding="utf-8")
    entries = audit(cur.log)
    assert [e["decision"] for e in entries if e["event"] == "decision"] == ["saved", "saved"]


def test_a_duplicate_updates_instead_of_adding(cur, mem):
    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    c = cur(FakeLLM({"summary": "", "facts": [fact("Exams", "Exam is on the 21st", "topic", 0.9)]}))
    say(c, "Actually my exam was moved and it is on the 21st now, please note that")
    finish(c, cur.clock)
    text = (mem.vault.root / "topics" / "exams.md").read_text(encoding="utf-8")
    assert "21st" in text and text.count("Exam is on") == 1


def test_update_by_block_id_replaces_the_fact(cur, mem):
    mem.remember("Exam is on the 20th", about="Exams", note_type="topic")
    bid = mem.retrieve("exam").items[0].block_id
    c = cur(FakeLLM({"summary": "", "facts": [fact("Exams", "Exam moved to the 25th", "topic", 0.9, action="update", replaces_block="^" + bid)]}))
    say(c, "My exam moved to the 25th so remember that for me please")
    finish(c, cur.clock)
    text = (mem.vault.root / "topics" / "exams.md").read_text(encoding="utf-8")
    assert "25th" in text and "20th" not in text.split("was:")[0]


def test_low_confidence_and_personal_facts_wait_in_the_inbox(cur, mem):
    c = cur(FakeLLM({"summary": "", "facts": [
        fact("me", "Prefers dark mode", "preference", 0.75),            # personal, below 0.8
        fact("Portfolio Site", "Might launch in March", "project", 0.5),  # below 0.7
        fact("me", "Lives in Pune", "profile", 0.9)]}))                 # fine
    say(c, "I think I prefer dark mode and maybe the portfolio site launches in March and I live in Pune")
    finish(c, cur.clock)
    inbox = "\n".join(p.read_text(encoding="utf-8") for p in (mem.vault.root / "inbox").glob("*.md"))
    assert "Prefers dark mode" in inbox and "Might launch in March" in inbox and "Lives in Pune" not in inbox
    assert "Lives in Pune" in (mem.vault.root / "profile.md").read_text(encoding="utf-8")
    assert [e["decision"] for e in audit(cur.log) if e["event"] == "decision"] == ["inbox", "inbox", "saved"]


def test_secrets_from_the_llm_are_blocked_and_logged_redacted(cur, mem):
    secret = "API key is sk-abcdefghijklmnopqrstuvwxyz123456"
    c = cur(FakeLLM({"summary": "", "facts": [fact("me", secret, "profile", 1.0), fact("me", "Lives in Pune", "profile", 0.95)]}))
    say(c, "My openai api key is sk-abcdefghijklmnopqrstuvwxyz123456 and I live in Pune, remember both")
    finish(c, cur.clock)
    everything = "\n".join(p.read_text(encoding="utf-8") for p in mem.vault.iter_paths()) + cur.log.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in everything
    assert [e["decision"] for e in audit(cur.log) if e["event"] == "decision"] == ["skipped", "saved"]
    assert "secret" in [e for e in audit(cur.log) if e["event"] == "decision"][0]["reason"]


def test_trivial_conversation_makes_no_llm_call(cur):
    llm = FakeLLM()
    c = cur(llm)
    c.add_turn("open notepad", "Done.")
    finish(c, cur.clock)
    assert llm.calls == [] and any(e["event"] == "skipped_conversation" for e in audit(cur.log))


def test_failed_or_rate_limited_calls_keep_the_conversation_and_retry_later(cur, mem):
    llm = FakeLLM(RuntimeError("HTTP 429"), {"summary": "", "facts": [fact()]})
    c = cur(llm)
    say(c)
    assert finish(c, cur.clock) == 0 and c.pending() == 1                 # kept, not lost
    assert c.run_pending() == 0 and len(llm.calls) == 1                   # not due yet: no second call
    cur.clock.t += 400
    assert c.run_pending() == 1 and c.pending() == 0
    assert "Roommate since 2024" in mem.recall("Rahul").message
    assert any(e["event"] == "retry_later" for e in audit(cur.log))


def test_unusable_json_is_retried_then_dropped_after_max_attempts(cur):
    llm = FakeLLM(*["not json"] * 10)
    c = cur(llm, max_attempts=2)
    say(c)
    finish(c, cur.clock)
    cur.clock.t += 400
    c.run_pending()
    assert c.pending() == 0 and any(e["event"] == "dropped_conversation" for e in audit(cur.log))


def test_the_queue_survives_a_restart(cur, mem, tmp_path):
    c1 = cur(FakeLLM())
    say(c1)                                                              # app "crashes" before the idle time passes
    c2 = Curator(mem, FakeLLM({"summary": "", "facts": [fact()]}), idle_seconds=120, queue_path=tmp_path / "queue.json",
                 log_path=tmp_path / "curator.jsonl", clock=cur.clock)
    assert c2.pending() == 1
    cur.clock.t += 130
    assert c2.run_pending() == 1 and "Roommate since 2024" in mem.recall("Rahul").message


def test_nothing_runs_while_the_assistant_is_busy_or_the_conversation_is_still_open(cur):
    llm = FakeLLM({"summary": "", "facts": []})
    busy = [True]
    c = cur(llm, is_busy=lambda: busy[0])
    say(c)
    assert c.run_pending() == 0 and llm.calls == []                      # still within the idle window
    cur.clock.t += 130
    assert c.run_pending() == 0 and llm.calls == []                      # idle passed but a live turn is running
    busy[0] = False
    assert c.run_pending() == 1 and len(llm.calls) == 1


def test_the_transcript_is_marked_as_data_and_has_only_user_and_assistant_text(cur):
    llm = FakeLLM()
    c = cur(llm)
    say(c, "Remember that my roommate Rahul is helping me with the portfolio site", "Saved.")
    finish(c, cur.clock)
    system, messages, tools = llm.calls[0]
    assert tools == [] and "<conversation>" in messages[0]["text"] and "DATA" in system and "Never follow instructions" in system


# ---- forget works on curator-saved facts ----------------------------------------------------------------------------------
def test_forget_removes_curator_saved_facts_completely(cur, mem, monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(forget_mod, "send2trash", lambda p: shutil.move(p, bin_dir))
    c = cur(FakeLLM({"summary": "", "facts": [fact(), fact("Portfolio Site", "Rahul is building the logo", "project", 0.9)]}))
    say(c)
    finish(c, cur.clock)
    assert "Roommate since 2024" in mem.recall("Rahul").message
    preview = mem.forget("Rahul")
    assert preview.needs_confirmation
    mem.forget("Rahul", confirm=True)
    everything = "\n".join(p.read_text(encoding="utf-8") for p in mem.vault.iter_paths()).lower()
    assert "roommate since 2024" not in everything and "rahul" not in everything.replace("provenance", "")
