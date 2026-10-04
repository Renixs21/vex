"""append_to_file, replace_in_file, overwrite_file, and create_file on an existing file (replace, or save a new copy)."""
import json
from pathlib import Path

import pytest
from helpers import NO, VOICE_YES, YES, ScriptedConfirmer

import tools
from config import settings
from safety import undo as undo_mod
from safety.audit import AuditLog
from safety.executor import Executor
from safety.undo import UndoStack
from tools import files, registry

tools.load_all()


@pytest.fixture
def world(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    monkeypatch.setitem(settings["safety"], "extra_safe_folders", [str(safe)])
    monkeypatch.setattr("config.ROOT", tmp_path)                                   # undo backups go to tmp_path/data
    stack = UndoStack(tmp_path / "undo.json")
    undo_mod.configure(stack)
    trashed = []
    monkeypatch.setattr("tools.files.send2trash", lambda p: trashed.append(p))
    monkeypatch.setattr("safety.undo.send2trash", lambda p: (trashed.append(p), Path(p).unlink()))

    def executor(*answers):
        conf = ScriptedConfirmer(*answers)
        return Executor(conf, AuditLog(tmp_path / "actions.jsonl"), stack), conf

    return safe, executor, stack, trashed


def audit_rows(tmp_path):
    return [json.loads(l) for l in (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()]


# ---- append ---------------------------------------------------------------------------------------------------------------------
def test_append_shows_a_preview_asks_and_adds_a_new_line(world):
    safe, executor, _, _ = world
    f = safe / "notes.txt"
    f.write_text("buy milk", encoding="utf-8")
    ex, conf = executor(YES)
    r = ex.run("append_to_file", {"path": str(f), "text": "call mom"})
    assert r.ok and f.read_text(encoding="utf-8") == "buy milk\ncall mom"
    req = conf.requests[0]
    assert req.tier == "yellow" and not req.requires_non_voice
    assert req.detail.splitlines()[0] == "Add to the end of notes.txt (8 characters)"
    assert "+call mom" in req.detail


def test_append_keeps_crlf_files_crlf_and_adds_no_blank_line(world):
    safe, executor, _, _ = world
    f = safe / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    ex, _ = executor(YES)
    assert ex.run("append_to_file", {"path": str(f), "text": "three"}).ok
    assert f.read_bytes() == b"one\r\ntwo\r\nthree"


def test_declining_changes_nothing(world):
    safe, executor, stack, _ = world
    f = safe / "a.txt"
    f.write_text("keep me", encoding="utf-8")
    ex, _ = executor(NO)
    assert not ex.run("append_to_file", {"path": str(f), "text": "x"}).ok and not ex.run("overwrite_file", {"path": str(f), "content": "x"}).ok
    assert f.read_text(encoding="utf-8") == "keep me" and stack.peek() is None


def test_a_spoken_yes_is_enough_for_a_yellow_edit(world):
    safe, executor, _, _ = world
    f = safe / "a.txt"
    f.write_text("x", encoding="utf-8")
    ex, _ = executor(VOICE_YES)
    assert ex.run("append_to_file", {"path": str(f), "text": "y"}).ok


# ---- replace ---------------------------------------------------------------------------------------------------------------------
def test_replace_changes_one_unique_piece_with_a_diff_preview(world):
    safe, executor, _, _ = world
    f = safe / "plan.txt"
    f.write_text("meeting at 4pm\nlunch at 1pm\n", encoding="utf-8")
    ex, conf = executor(YES)
    r = ex.run("replace_in_file", {"path": str(f), "old_text": "4pm", "new_text": "5pm"})
    assert r.ok and f.read_text(encoding="utf-8") == "meeting at 5pm\nlunch at 1pm\n"
    d = conf.requests[0].detail
    assert d.splitlines()[0] == "Change text in plan.txt" and "-meeting at 4pm" in d and "+meeting at 5pm" in d


def test_replace_refuses_missing_or_ambiguous_text_and_replace_all_works(world):
    safe, executor, _, _ = world
    f = safe / "p.txt"
    f.write_text("a\nb\na\n", encoding="utf-8")
    ex, conf = executor(YES, YES)
    miss = ex.run("replace_in_file", {"path": str(f), "old_text": "zzz", "new_text": "y"})
    assert not miss.ok and "couldn't find" in miss.message and conf.requests == []
    amb = ex.run("replace_in_file", {"path": str(f), "old_text": "a", "new_text": "c"})
    assert not amb.ok and "2 times" in amb.message and f.read_text(encoding="utf-8") == "a\nb\na\n"
    assert ex.run("replace_in_file", {"path": str(f), "old_text": "a", "new_text": "c", "replace_all": True}).ok
    assert f.read_text(encoding="utf-8") == "c\nb\nc\n"


# ---- overwrite -------------------------------------------------------------------------------------------------------------------
def test_overwrite_replaces_everything_and_shows_what_goes(world):
    safe, executor, _, _ = world
    f = safe / "o.txt"
    f.write_text("old line\n", encoding="utf-8")
    ex, conf = executor(YES)
    assert ex.run("overwrite_file", {"path": str(f), "content": "new line\n"}).ok and f.read_text(encoding="utf-8") == "new line\n"
    d = conf.requests[0].detail
    assert d.splitlines()[0] == "Replace the whole content of o.txt (9 characters)" and "-old line" in d and "+new line" in d


def test_edit_tools_need_an_existing_text_file(world):
    safe, executor, _, _ = world
    ex, conf = executor(YES, YES, YES)
    assert "doesn't exist" in ex.run("append_to_file", {"path": str(safe / "nope.txt"), "text": "x"}).message
    b = safe / "pic.bin"
    b.write_bytes(b"\x89PNG\x00\x00binary")
    assert "binary" in ex.run("overwrite_file", {"path": str(b), "content": "x"}).message
    assert "not a file" in ex.run("append_to_file", {"path": str(safe), "text": "x"}).message
    assert conf.requests == []


# ---- protected places ------------------------------------------------------------------------------------------------------------
def test_protected_and_secret_files_are_never_edited_even_if_you_say_yes(world, tmp_path):
    safe, executor, _, _ = world
    ex, conf = executor(YES, YES, YES)
    hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    r1 = ex.run("append_to_file", {"path": str(hosts), "text": "127.0.0.1 evil.example"})
    env = safe / ".env"
    env.write_text("KEY=1", encoding="utf-8")
    r2 = ex.run("overwrite_file", {"path": str(env), "content": "KEY=2"})
    ssh = tmp_path / ".ssh" / "config"                                              # any folder named .ssh counts as credentials
    ssh.parent.mkdir()
    ssh.write_text("Host x", encoding="utf-8")
    r3 = ex.run("replace_in_file", {"path": str(ssh), "old_text": "Host", "new_text": "Port"})
    assert not r1.ok and not r2.ok and not r3.ok
    assert env.read_text(encoding="utf-8") == "KEY=1" and conf.requests == []
    assert [r["decision"] for r in audit_rows(tmp_path)][-3:] == ["blocked", "blocked", "blocked"]


def test_editing_outside_the_safe_folders_asks_and_says_so(world, tmp_path):
    safe, executor, _, _ = world
    other = tmp_path / "elsewhere"
    other.mkdir()
    f = other / "x.txt"
    f.write_text("a", encoding="utf-8")
    ex, conf = executor(YES)
    assert ex.run("append_to_file", {"path": str(f), "text": "b"}).ok
    assert conf.requests[0].tier == "yellow" and "outside the safe folders" in " ".join(conf.requests[0].warnings)


# ---- undo -------------------------------------------------------------------------------------------------------------------------
def test_undo_that_restores_the_text_from_before_the_edit(world):
    safe, executor, stack, _ = world
    f = safe / "u.txt"
    f.write_text("original\n", encoding="utf-8")
    ex, _ = executor(YES, YES)
    ex.run("append_to_file", {"path": str(f), "text": "added"})
    ex.run("replace_in_file", {"path": str(f), "old_text": "original", "new_text": "changed"})
    assert f.read_text(encoding="utf-8") == "changed\nadded"
    ok, msg, _ = stack.undo_last()
    assert ok and "Restored the earlier version" in msg and f.read_text(encoding="utf-8") == "original\nadded"
    ok, _, _ = stack.undo_last()
    assert ok and f.read_text(encoding="utf-8") == "original\n"
    assert stack.undo_last()[0] is False                                                       # nothing left


def test_undo_refuses_when_the_file_changed_again_after_the_edit(world):
    safe, executor, stack, _ = world
    f = safe / "u.txt"
    f.write_text("one", encoding="utf-8")
    ex, _ = executor(YES)
    ex.run("append_to_file", {"path": str(f), "text": "two"})
    f.write_text("someone else edited this", encoding="utf-8")
    ok, msg, _ = stack.undo_last()
    assert not ok and "changed since my edit" in msg and f.read_text(encoding="utf-8") == "someone else edited this"


def test_the_undo_tool_reports_edits_and_the_audit_log_marks_them_undoable(world, tmp_path):
    safe, executor, stack, _ = world
    f = safe / "u.txt"
    f.write_text("one", encoding="utf-8")
    ex, _ = executor(YES)
    ex.run("overwrite_file", {"path": str(f), "content": "two"})
    assert audit_rows(tmp_path)[-1]["undoable"] is True
    r = ex.run("undo_last", {})
    assert r.ok and f.read_text(encoding="utf-8") == "one"


# ---- create_file on an existing file: replace or save a copy -----------------------------------------------------------------------
def test_create_on_an_existing_file_asks_replace_yes_replaces_and_keeps_the_old_version(world):
    safe, executor, stack, _ = world
    f = safe / "names.txt"
    f.write_text("old names", encoding="utf-8")
    ex, conf = executor(YES)
    r = ex.run("create_file", {"path": str(f), "content": "new names"})
    assert r.ok and f.read_text(encoding="utf-8") == "new names"
    req = conf.requests[0]
    assert req.tier == "yellow" and req.detail.splitlines()[0] == "names.txt already exists. Replace it (No saves a copy as names (2).txt)"
    assert "-old names" in req.detail and "+new names" in req.detail
    ok, _, _ = stack.undo_last()
    assert ok and f.read_text(encoding="utf-8") == "old names"                                 # undo brings the old one back


def test_create_on_an_existing_file_declined_saves_it_as_a_new_name_instead(world, tmp_path):
    safe, executor, stack, _ = world
    f = safe / "names.txt"
    f.write_text("old names", encoding="utf-8")
    (safe / "names (2).txt").write_text("taken", encoding="utf-8")
    ex, conf = executor(NO)
    r = ex.run("create_file", {"path": str(f), "content": "new names"})
    assert r.ok and "names (3).txt" in r.message                                              # the first free number
    assert f.read_text(encoding="utf-8") == "old names" and (safe / "names (3).txt").read_text(encoding="utf-8") == "new names"
    assert len(conf.requests) == 1                                                            # the copy needed no second question
    rows = audit_rows(tmp_path)
    assert [(r["tool"], r["decision"]) for r in rows[-2:]] == [("create_file", "denied"), ("create_file", "auto")]
    assert rows[-2]["alternative"] is True
    stack.undo_last()
    assert not (safe / "names (3).txt").exists() and f.exists()                               # undo removes only the copy


def test_a_timeout_is_not_a_no_and_creates_nothing(world):
    from helpers import TIMEOUT

    safe, executor, _, _ = world
    f = safe / "names.txt"
    f.write_text("old", encoding="utf-8")
    ex, _ = executor(TIMEOUT)
    assert not ex.run("create_file", {"path": str(f), "content": "new"}).ok
    assert f.read_text(encoding="utf-8") == "old" and not (safe / "names (2).txt").exists()


def test_create_on_a_new_path_is_still_green_and_never_asks(world):
    safe, executor, _, _ = world
    ex, conf = executor()
    assert ex.run("create_file", {"path": str(safe / "fresh.txt"), "content": "hi"}).ok and conf.requests == []


def test_the_model_cannot_ask_for_a_replace_itself(world):
    safe, executor, _, _ = world
    f = safe / "names.txt"
    f.write_text("old", encoding="utf-8")
    ex, conf = executor(NO)
    ex.run("create_file", {"path": str(f), "content": "new", "_replace": True})               # an undeclared argument is dropped
    assert f.read_text(encoding="utf-8") == "old"
    assert "_replace" not in registry.get("create_file").parameters["properties"]
