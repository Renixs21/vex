import json
import shutil
from datetime import datetime, timedelta

import pytest

from memory.consolidate import Consolidator
from memory.vault import Vault


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime.now()

    def __call__(self):
        return self.now


@pytest.fixture
def con(mem, tmp_path):
    clock = Clock()
    trash = tmp_path / "trash"
    trash.mkdir()
    c = Consolidator(mem, log_path=tmp_path / "cons.jsonl", state_path=tmp_path / "state.json", clock=clock,
                     trash=lambda p: shutil.move(p, trash))
    c.clock, c.trash_dir = clock, trash
    return c


def log_events(c):
    return [json.loads(l) for l in c.log_path.read_text(encoding="utf-8").splitlines()] if c.log_path.exists() else []


def text_of(mem, rel):
    return (mem.vault.root / rel).read_text(encoding="utf-8")


def add_raw(mem, title, type_, *facts):
    """Facts written straight into a note (bypassing remember's own near-duplicate update)."""
    n = mem.vault.create(type_, title)
    for f in facts:
        mem.vault.mutate(n.path, lambda note, f=f: Vault.add_fact(note, f, "test"))
    mem.index.sync()
    return n.path


# ---- 1. duplicates --------------------------------------------------------------------------------------------------------
def test_near_duplicates_are_merged_and_the_old_wording_is_kept_in_the_log(con, mem):
    add_raw(mem, "Coffee", "topic", "Likes strong black coffee in the morning", "Likes strong black coffee in the mornings",
            "Exam is on the 20th", "Exam is on the 21st")
    r = con.run()
    assert r["counts"]["merged"] == 1
    text = text_of(mem, "topics/coffee.md")
    assert text.count("- Likes strong black coffee") == 1
    assert "Exam is on the 20th" in text and "Exam is on the 21st" in text                 # numbers differ: never merged
    merged = [e for e in log_events(con) if e["event"] == "merge_duplicate"][0]
    assert "morning" in merged["removed_text"]


def test_dry_run_reports_but_changes_nothing(con, mem):
    path = add_raw(mem, "Coffee", "topic", "Likes strong black coffee in the morning", "Likes strong black coffee in the mornings")
    before = path.read_text(encoding="utf-8")
    r = con.run(dry_run=True)
    assert r["counts"]["merged"] == 1 and path.read_text(encoding="utf-8") == before
    assert not con.log_path.exists() and "last_run" not in con.state


# ---- 2. inbox -------------------------------------------------------------------------------------------------------------
def inbox_text(mem):
    return "\n".join(p.read_text(encoding="utf-8") for p in (mem.vault.root / "inbox").glob("*.md"))


def test_repeated_project_proposals_are_promoted_but_personal_sensitive_and_single_ones_stay(con, mem):
    mem.add_to_inbox("Deadline is the 20th of March (proposed for Portfolio Site, project, confidence 0.65; low confidence (0.65))")
    mem.add_to_inbox("Deadline is the 20th of March. (proposed for Portfolio Site, project, confidence 0.62; low confidence (0.62))")
    mem.add_to_inbox("Prefers dark mode (proposed for me, profile, confidence 0.75; personal fact below 0.8 (0.75))")
    mem.add_to_inbox("Prefers dark mode! (proposed for me, profile, confidence 0.75; personal fact below 0.8 (0.75))")
    mem.add_to_inbox("Rahul has cancer (proposed for Rahul, person, confidence 0.9; sensitive detail about another person)")
    mem.add_to_inbox("Rahul has cancer. (proposed for Rahul, person, confidence 0.9; sensitive detail about another person)")
    mem.add_to_inbox("Logo is blue (proposed for Portfolio Site, project, confidence 0.65; low confidence (0.65))")
    mem.add_to_inbox("Old note (proposed deletion for Rahul, person, confidence 0.9; deletions are proposed, never applied automatically)")
    r = con.run()
    assert r["counts"]["promoted"] == 1
    assert "Deadline is the 20th of March" in text_of(mem, "projects/portfolio-site.md")
    left = inbox_text(mem)
    assert "Deadline is the 20th" not in left                                                   # moved, not duplicated
    for stays in ("Prefers dark mode", "Rahul has cancer", "Logo is blue", "Old note"):
        assert stays in left
    profile = mem.vault.root / "profile.md"
    assert not profile.exists() or "dark mode" not in profile.read_text(encoding="utf-8")


# ---- 3. daily notes -------------------------------------------------------------------------------------------------------
def test_old_daily_notes_become_a_weekly_note_and_recent_ones_stay(con, mem):
    now = con.clock.now
    old = [(now - timedelta(days=60 + i)).date() for i in range(3)]
    old_files = []
    for d in old:
        old_files.append(add_raw(mem, d.isoformat(), "daily", f"Talked about [[Portfolio Site]] on {d}"))
    recent = add_raw(mem, (now - timedelta(days=3)).date().isoformat(), "daily", "Recent chat")
    add_raw(mem, "Portfolio Site", "project", "Built with Next.js")
    r = con.run()
    assert r["counts"]["compressed"] == 3
    assert all(not p.exists() for p in old_files) and recent.exists()
    assert len(list(con.trash_dir.iterdir())) == 3                                              # Recycle Bin, restorable
    weekly = [p for p in (mem.vault.root / "daily").glob("*.md") if "-w" in p.stem.lower()]
    assert weekly
    body = "\n".join(p.read_text(encoding="utf-8") for p in weekly)
    for d in old:
        assert f"{d.isoformat()}: Talked about [[Portfolio Site]]" in body                      # lines and links kept
    r2 = con.run()
    assert r2["counts"]["compressed"] == 0                                                      # idempotent


# ---- 4. importance --------------------------------------------------------------------------------------------------------
def test_unused_notes_lose_importance_slowly_and_facts_are_never_deleted(con, mem):
    mem.remember("Roommate since 2024", about="Rahul", note_type="person")
    mem.remember("Lives in Pune", about="me", note_type="profile")
    mem.remember("Likes tea", note_type="preference")
    facts_before = mem.index.stats()["facts"]
    con.clock.now = datetime.now() + timedelta(days=120)
    r = con.run()
    assert r["counts"]["decayed"] == 1                                                          # singletons are exempt
    assert "importance: 2" in text_of(mem, "people/rahul.md")
    assert mem.index.stats()["facts"] == facts_before
    con.clock.now += timedelta(days=5)
    assert con.run()["counts"]["decayed"] == 0                                                  # at most once a month
    con.clock.now += timedelta(days=40)
    assert con.run()["counts"]["decayed"] == 1
    assert "importance: 1" in text_of(mem, "people/rahul.md")
    con.clock.now += timedelta(days=40)
    assert con.run()["counts"]["decayed"] == 0                                                  # never below 1


def test_recently_used_notes_do_not_decay(con, mem):
    mem.remember("Roommate since 2024", about="Rahul", note_type="person")
    con.clock.now = datetime.now() + timedelta(days=120)
    mem.index.touch([mem.index.resolve("Rahul")["id"]], (con.clock.now - timedelta(days=5)).isoformat())
    assert con.run()["counts"]["decayed"] == 0


# ---- schedule / log -------------------------------------------------------------------------------------------------------
def test_due_once_a_day_after_3am_or_on_the_next_start_if_missed(con):
    c = con.clock
    c.now = datetime(2026, 10, 4, 2, 0)
    assert con.due()                                  # never ran: run on first start
    con.state["last_run"] = "2026-10-03"
    assert not con.due()                              # ran yesterday and it is before 03:00
    c.now = datetime(2026, 10, 4, 3, 5)
    assert con.due()
    con.state["last_run"] = "2026-10-04"
    assert not con.due()
    c.now = datetime(2026, 10, 7, 1, 0)               # PC was off for days: due at the next start even before 03:00
    assert con.due()


def test_every_change_is_logged_and_a_failing_step_does_not_stop_the_others(con, mem, monkeypatch):
    add_raw(mem, "Coffee", "topic", "Likes strong black coffee in the morning", "Likes strong black coffee in the mornings")
    mem.remember("Roommate since 2024", about="Rahul", note_type="person")
    monkeypatch.setattr(con, "promote_inbox", lambda: 1 / 0)
    con.clock.now = datetime.now() + timedelta(days=120)
    r = con.run()
    events = [e["event"] for e in log_events(con)]
    assert r["counts"]["promoted"] == 0 and r["counts"]["merged"] == 1 and r["counts"]["decayed"] >= 1
    assert "merge_duplicate" in events and "decay_importance" in events and "error" in events and events[-1] == "run"
