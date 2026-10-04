"""Housekeeping: rotates and caps Vex's own files, logs every change, never touches your notes; the vault soft cap pauses the curator."""
import gzip
import os
import time
from pathlib import Path

import pytest

from ui import housekeeping as hk_mod
from ui.housekeeping import MB, Housekeeper

NOW = 1_780_000_000.0                       # a fixed "now" (2026)


def make(tmp_path, **kw):
    root = tmp_path / "vex"
    for d in ("logs", "data", "VexVault/.vexmind", "VexVault/people", "VexVault/daily"):
        (root / d).mkdir(parents=True, exist_ok=True)
    notes = {"VexVault/profile.md": "me", "VexVault/people/Rahul.md": "roommate", "VexVault/daily/2025-01-01.md": "old day"}
    for rel, text in notes.items():
        (root / rel).write_text(text, encoding="utf-8")
        os.utime(root / rel, (NOW - 400 * 86400, NOW - 400 * 86400))       # very old notes: still never touched
    return Housekeeper(root, clock=lambda: NOW, **kw), root


def big(path: Path, mb: float, line: bytes = b'{"tool":"x"}\n') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(mb * MB / len(line)) + 1
    path.write_bytes(line * n)


def old(path: Path, days: float = 40) -> None:
    os.utime(path, (NOW - days * 86400, NOW - days * 86400))


def vault_snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime) for p in (root / "VexVault").rglob("*.md")}


# ---- the audit log ------------------------------------------------------------------------------------------------------------------
def test_the_audit_log_is_archived_at_5_mb_into_a_monthly_gzip(tmp_path):
    hk, root = make(tmp_path)
    big(root / "logs/actions.jsonl", 5.1)
    done = hk.run()
    arch = root / "logs/archive" / f"actions-{time.strftime('%Y-%m', time.localtime(NOW))}.jsonl.gz"
    assert arch.exists() and not (root / "logs/actions.jsonl").exists()
    assert gzip.decompress(arch.read_bytes()).startswith(b'{"tool":"x"}')
    assert any(d.startswith("ARCHIVED logs") for d in done)
    assert "ARCHIVED" in (root / "logs/housekeeping.log").read_text(encoding="utf-8")


def test_a_second_archive_in_the_same_month_is_appended_not_overwritten(tmp_path):
    hk, root = make(tmp_path)
    big(root / "logs/actions.jsonl", 5.1, b'{"n":1}\n')
    hk.run()
    big(root / "logs/actions.jsonl", 5.1, b'{"n":2}\n')
    hk.run()
    arch = next((root / "logs/archive").glob("actions-*.jsonl.gz"))
    data = gzip.decompress(arch.read_bytes())
    assert data.count(b'{"n":1}') > 0 and data.count(b'{"n":2}') > 0


def test_a_small_audit_log_is_left_alone(tmp_path):
    hk, root = make(tmp_path)
    (root / "logs/actions.jsonl").write_text('{"a":1}\n', encoding="utf-8")
    assert hk.run() == [] and (root / "logs/actions.jsonl").exists()
    assert not (root / "logs/housekeeping.log").exists()                     # nothing done, nothing logged


def test_audit_archives_are_capped_at_50_mb_oldest_month_first(tmp_path, monkeypatch):
    monkeypatch.setattr(hk_mod, "ACTIONS_ARCHIVE_CAP_MB", 0.002)
    hk, root = make(tmp_path)
    a = root / "logs/archive"
    a.mkdir(parents=True)
    for month in ("2026-01", "2026-02", "2026-03"):
        (a / f"actions-{month}.jsonl.gz").write_bytes(os.urandom(1500))
    done = hk.run()
    assert sorted(p.name for p in a.iterdir()) == ["actions-2026-03.jsonl.gz"]
    assert sum("REMOVED old audit archive" in d for d in done) == 2


def test_a_busy_log_is_skipped_and_retried_later(tmp_path, monkeypatch):
    hk, root = make(tmp_path)
    big(root / "logs/actions.jsonl", 5.1)
    monkeypatch.setattr(Housekeeper, "_take", lambda self, p: None)          # Windows: someone has it open right now
    assert hk.run() == [] and (root / "logs/actions.jsonl").exists()


# ---- the other jsonl logs ---------------------------------------------------------------------------------------------------------------
def test_curator_consolidation_and_latency_logs_rotate_at_2_mb_keeping_3(tmp_path):
    hk, root = make(tmp_path)
    for i in range(5):
        big(root / "logs/curator.jsonl", 2.1, f'{{"gen":{i}}}\n'.encode())
        hk.run()
    gens = sorted(p.name for p in (root / "logs").glob("curator.*.jsonl.gz"))
    assert gens == ["curator.1.jsonl.gz", "curator.2.jsonl.gz", "curator.3.jsonl.gz"]
    assert b'"gen":4' in gzip.decompress((root / "logs/curator.1.jsonl.gz").read_bytes())
    assert b'"gen":2' in gzip.decompress((root / "logs/curator.3.jsonl.gz").read_bytes())
    big(root / "logs/latency.jsonl", 2.1)
    big(root / "logs/memory_consolidation.jsonl", 2.1)
    done = hk.run()
    assert len(done) == 2 and any("latency.jsonl" in d for d in done) and any("memory_consolidation.jsonl" in d for d in done)


def test_the_latency_report_still_reads_the_live_file_after_rotation(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
    import latency_report

    hk, root = make(tmp_path)
    big(root / "logs/latency.jsonl", 2.1, b'{"ts":"2026-10-04T18:00:00","stages":{}}\n')
    hk.run()
    (root / "logs/latency.jsonl").write_text('{"ts":"2026-10-05T09:00:00","stages":{}}\n', encoding="utf-8")
    assert len(latency_report.load(root / "logs/latency.jsonl")) == 1          # the .gz generations are not mixed in as text


# ---- strays, backups -------------------------------------------------------------------------------------------------------------------------
def test_old_stray_files_are_removed_recent_ones_and_real_logs_kept(tmp_path):
    hk, root = make(tmp_path)
    logs = root / "logs"
    for name in ("console.txt", "app_stdout.txt", "bench_after_groq.json", "eval_models.json", "child_stderr.old.txt", "x.tmp"):
        (logs / name).write_text("x", encoding="utf-8")
        old(logs / name)
    (logs / "bench_new.json").write_text("x", encoding="utf-8")                     # recent: kept
    for keep in ("vex.log", "supervisor.log", "health.txt", "undo.json"):
        (logs / keep).write_text("x", encoding="utf-8")
        old(logs / keep, 400)                                                          # old but not stray: kept
    (root / "data/reminders.json.tmp").write_text("x", encoding="utf-8")
    old(root / "data/reminders.json.tmp")
    (root / "VexVault/.vexmind/index.tmp").write_text("x", encoding="utf-8")
    old(root / "VexVault/.vexmind/index.tmp")
    done = hk.run()
    left = sorted(p.name for p in logs.iterdir())
    assert left == ["bench_new.json", "health.txt", "housekeeping.log", "supervisor.log", "undo.json", "vex.log"]
    assert not (root / "data/reminders.json.tmp").exists() and not (root / "VexVault/.vexmind/index.tmp").exists()
    assert sum(d.startswith("REMOVED") for d in done) == 8


def test_undo_backups_are_capped_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(hk_mod, "UNDO_CAP_MB", 0.003)
    hk, root = make(tmp_path)
    d = root / "data/undo_backups"
    d.mkdir()
    for i in range(5):
        (d / f"{i}.bak").write_bytes(b"x" * 1000)
        os.utime(d / f"{i}.bak", (NOW - (10 - i) * 3600, NOW - (10 - i) * 3600))
    done = hk.run()
    assert sorted(p.name for p in d.iterdir()) == ["2.bak", "3.bak", "4.bak"]
    assert any("2 oldest undo backup(s)" in x and "can no longer be undone" in x for x in done)


# ---- the vault -----------------------------------------------------------------------------------------------------------------------------
def test_notes_are_never_touched_whatever_their_age_or_the_vaults_size(tmp_path, monkeypatch):
    hk, root = make(tmp_path, vault_cap_mb=0.001)                                # far over the cap
    (root / "VexVault/stray.tmp").write_text("x", encoding="utf-8")              # outside .vexmind: not ours to remove
    old(root / "VexVault/stray.tmp")
    before = vault_snapshot(root)
    hk.run()
    assert vault_snapshot(root) == before and (root / "VexVault/stray.tmp").exists()


def test_vault_warns_at_80_percent_once_a_day(tmp_path):
    shown = []
    hk, root = make(tmp_path, notify=lambda t, m: shown.append((t, m)))
    (root / "VexVault/big.md").write_bytes(b"x" * 850)
    hk.vault_cap_mb = 0.001                                                       # 1000 bytes: ~86% used
    done = hk.run()
    hk.run()
    assert len(shown) == 1 and "getting big" in shown[0][0] and "Nothing is deleted automatically" in shown[0][1]
    assert any(d.startswith("VAULT") and "paused" not in d for d in done) and not hk.vault_full()


def test_at_100_percent_automatic_entries_pause(tmp_path):
    shown = []
    hk, root = make(tmp_path, notify=lambda t, m: shown.append((t, m)))
    (root / "VexVault/big.md").write_bytes(b"x" * 2000)
    hk.vault_cap_mb = 0.001
    done = hk.run()
    assert hk.vault_full() and "full" in shown[0][0] and "paused" in shown[0][1] and any("automatic entries paused" in d for d in done)


def test_vault_size_is_cached_for_the_curators_frequent_questions(tmp_path):
    t = [NOW]
    hk, root = make(tmp_path)
    hk._clock = lambda: t[0]
    first = hk.vault_bytes()
    (root / "VexVault/new.md").write_bytes(b"x" * 5000)
    assert hk.vault_bytes() == first
    t[0] += 601
    assert hk.vault_bytes() == first + 5000


def test_the_curator_pauses_while_the_vault_is_full_and_keeps_conversations(mem, tmp_path):
    from memory.curator import Curator

    full = [True]

    class NoLLM:
        def generate(self, *a, **k):
            raise AssertionError("no LLM call while the vault is full")

    cur = Curator(mem, NoLLM(), idle_seconds=0, min_words=1, log_path=tmp_path / "curator.jsonl", vault_full=lambda: full[0])
    cur.add_turn("my exam is on the 20th of november", "Noted.")
    assert cur.run_pending(force_close=True) == 0 and cur.pending() == 1
    assert '"paused"' in (tmp_path / "curator.jsonl").read_text(encoding="utf-8")
    full[0] = False
    cur._llm = None
    assert not cur._vault_over_cap()


# ---- dry run, CLI, never raising ---------------------------------------------------------------------------------------------------------------
def test_dry_run_changes_nothing_and_logs_nothing(tmp_path):
    hk, root = make(tmp_path)
    big(root / "logs/actions.jsonl", 5.1)
    (root / "logs/console.txt").write_text("x", encoding="utf-8")
    old(root / "logs/console.txt")
    done = hk.run(dry_run=True)
    assert all(d.startswith("WOULD ") for d in done) and len(done) == 2
    assert (root / "logs/actions.jsonl").exists() and (root / "logs/console.txt").exists()
    assert not (root / "logs/housekeeping.log").exists()


def test_a_failing_step_never_stops_the_others(tmp_path, monkeypatch):
    hk, root = make(tmp_path)
    (root / "logs/console.txt").write_text("x", encoding="utf-8")
    old(root / "logs/console.txt")
    monkeypatch.setattr(Housekeeper, "rotate_actions", lambda self, a, d: 1 / 0)
    done = hk.run()
    assert any("console.txt" in d for d in done)


def test_cli_dry_run(capsys):
    assert hk_mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Vault:" in out and "soft cap" in out


def test_the_background_loop_runs_and_stops(tmp_path):
    hk, root = make(tmp_path)
    runs = []
    hk.run = lambda dry_run=False: runs.append(1) or []
    hk.start(first_delay=0.01, interval=0.01)
    deadline = time.time() + 2
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.01)
    hk.stop()
    assert len(runs) >= 2
