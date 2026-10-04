"""Nightly consolidation of VexMind. Conservative by design: it never silently loses what you told Vex.

  1. merge duplicates   near-identical facts inside one note are merged into one (the removed wording is kept in the surviving fact's
                        provenance and in the log); facts that differ in numbers/dates are never merged.
  2. promote the inbox  only project/topic facts that the curator proposed at least twice, with decent confidence, move into their
                        notes. Anything personal, sensitive, a proposed deletion, or proposed once stays for YOUR approval.
  3. compress daily     daily notes older than 30 days become one weekly note per ISO week (all lines and links kept); the
                        originals go to the Recycle Bin.
  4. decay importance   notes not used for 90 days lose one importance point (at most once a month, never below 1); the profile,
                        preferences and tasks are exempt. No fact is ever deleted by decay.

Everything it changes is appended to logs/memory_consolidation.jsonl. `run(dry_run=True)` only reports what it would do.
Runs about 03:00, or on the next start if the last run was missed, and only while Vex is idle.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz

from memory import forget as forget_mod
from memory import parser
from memory.api import SELF_NAMES, Memory, _only_numbers_differ
from memory.vault import SINGLETONS, Vault

log = logging.getLogger("vex.consolidate")

DUPLICATE_RATIO = 96
PROMOTE_MIN_PROPOSALS = 2
PROMOTE_MIN_CONFIDENCE = 0.6
PERSONAL_TYPES = {"profile", "preference", "person", "place"}
DAILY_KEEP_DAYS = 30
DECAY_AFTER_DAYS = 90
DECAY_EVERY_DAYS = 30
RUN_HOUR = 3
_DAILY_TITLE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROPOSAL = re.compile(r"^(?P<fact>.*?) \((?P<label>proposed|proposed deletion) for (?P<note>.*?), (?P<type>[\w-]+), "
                       r"confidence (?P<conf>[0-9.]+); (?P<reason>.*)\)$")


def _days_since(iso: str, now: datetime) -> float:
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return (now - d).total_seconds() / 86400.0


class Consolidator:
    def __init__(self, memory: Memory, *, log_path: Path | None = None, state_path: Path | None = None,
                 clock: Callable[[], datetime] = datetime.now, is_busy: Callable[[], bool] | None = None,
                 trash: Callable[[str], Any] | None = None):
        self.memory = memory
        self.log_path = log_path or (Path(__file__).resolve().parent.parent / "logs" / "memory_consolidation.jsonl")
        self.state_path = state_path or (memory.vault.root / ".vexmind" / "consolidate_state.json")
        self._clock = clock
        self.is_busy = is_busy or (lambda: False)
        self._trash = trash or (lambda p: forget_mod.send2trash(p))
        self._changes: list[dict] = []
        self._dry = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = self._load()

    # ---- state / log ------------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            log.warning("could not save the consolidation state", exc_info=True)

    def _record(self, event: str, **fields) -> None:
        entry = {"ts": self._clock().astimezone().isoformat(timespec="seconds"), "event": event, "dry_run": self._dry, **fields}
        self._changes.append(entry)
        if self._dry:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("could not write the consolidation log", exc_info=True)

    # ---- schedule -----------------------------------------------------------------------------------------------------
    def due(self) -> bool:
        """True once per day, after 03:00; also on the next start if the last run was missed."""
        now = self._clock()
        last = self.state.get("last_run")
        if last == now.date().isoformat():
            return False
        return now.hour >= RUN_HOUR or last is None or last < (now.date() - timedelta(days=1)).isoformat()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="vex-consolidate", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._stop.wait(90)                               # let Vex finish starting first
        while not self._stop.is_set():
            try:
                if self.due() and not self.is_busy():
                    self.run()
            except Exception:  # noqa: BLE001
                log.exception("consolidation failed")
            self._stop.wait(600)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ---- the run -----------------------------------------------------------------------------------------------------
    def run(self, dry_run: bool = False) -> dict:
        self._dry = dry_run
        self._changes = []
        self.memory.index.sync()
        steps = {"merged": self.merge_duplicates, "promoted": self.promote_inbox, "compressed": self.compress_daily,
                 "decayed": self.decay_importance}
        counts: dict[str, int] = {}
        for name, fn in steps.items():
            try:
                counts[name] = fn()
            except Exception:  # noqa: BLE001 - one step failing must not block the others
                log.exception("consolidation step %s failed", name)
                counts[name] = 0
                self._record("error", step=name)
            self.memory.index.sync()
        self._record("run", **counts)
        if not dry_run:
            self.state["last_run"] = self._clock().date().isoformat()
            self._save()
        log.info("consolidation %s: %s", "(dry run)" if dry_run else "done", counts)
        return {"counts": counts, "changes": list(self._changes), "dry_run": dry_run}

    # ---- 1. duplicates ------------------------------------------------------------------------------------------------
    def merge_duplicates(self) -> int:
        merged = 0
        for row in self.memory.index.list_notes():
            if row["type"] in ("daily", "inbox"):
                continue
            path = self.memory.vault.root / row["path"]
            note = self.memory.vault.read(path)
            blocks = note.parsed().blocks
            seen: dict[str, Any] = {}
            plan: list[tuple[Any, Any]] = []              # (kept, removed)
            gone: set[str] = set()
            for b in blocks:
                key = parser.normalize(parser.strip_links(b.text))
                if len(key.split()) < 3:
                    continue
                for other_key, other in seen.items():
                    if other.block_id in gone:
                        continue
                    if fuzz.ratio(key, other_key) >= DUPLICATE_RATIO and not _only_numbers_differ(key, other_key):
                        keep, drop = (other, b) if len(other.text) >= len(b.text) else (b, other)
                        plan.append((keep, drop))
                        gone.add(drop.block_id)
                        break
                else:
                    seen[key] = b
            if not plan:
                continue
            for keep, drop in plan:
                self._record("merge_duplicate", note=row["title"], path=row["path"], kept_block=keep.block_id, kept_text=keep.text,
                             removed_block=drop.block_id, removed_text=drop.text)
                merged += 1
            if self._dry:
                continue

            def apply(n, plan=plan):
                for keep, drop in plan:
                    Vault.update_fact(n, keep.block_id, keep.text, keep.source or "consolidation", was=f"merged duplicate: {drop.text[:80]}")
                Vault.delete_facts(n, {drop.block_id for _, drop in plan})

            self.memory.vault.mutate(path, apply)
        return merged

    # ---- 2. inbox -----------------------------------------------------------------------------------------------------
    def promote_inbox(self) -> int:
        groups: list[dict] = []
        for row in self.memory.index.list_notes("inbox"):
            note = self.memory.vault.read(self.memory.vault.root / row["path"])
            for b in note.parsed().blocks:
                m = _PROPOSAL.match(b.text.strip())
                if not m or m["label"] != "proposed":
                    continue
                fact = m["fact"].strip()
                key = parser.normalize(fact)
                g = next((g for g in groups if fuzz.ratio(key, g["key"]) >= 90), None)
                if g is None:
                    g = {"key": key, "fact": fact, "note": m["note"], "type": m["type"], "conf": 0.0, "reasons": [], "where": []}
                    groups.append(g)
                g["conf"] = max(g["conf"], float(m["conf"]))
                g["reasons"].append(m["reason"])
                g["where"].append((row["path"], b.block_id))
        promoted = 0
        for g in groups:
            personal = g["type"] in PERSONAL_TYPES or parser.normalize(g["note"]) in SELF_NAMES
            risky = any("sensitive" in r or "secret" in r for r in g["reasons"])
            if len(g["where"]) < PROMOTE_MIN_PROPOSALS or g["conf"] < PROMOTE_MIN_CONFIDENCE or personal or risky:
                continue
            self._record("promote_inbox", note=g["note"], note_type=g["type"], fact=g["fact"], proposals=len(g["where"]),
                         confidence=g["conf"])
            promoted += 1
            if self._dry:
                continue
            res = self.memory.remember(g["fact"], about=g["note"], note_type=g["type"], source="consolidation · inbox")
            if not res.ok:
                continue
            for path, bid in g["where"]:
                self.memory.vault.mutate(self.memory.vault.root / path, lambda n, bid=bid: Vault.delete_facts(n, {bid}))
        return promoted

    # ---- 3. daily notes -----------------------------------------------------------------------------------------------
    def compress_daily(self) -> int:
        cutoff = self._clock().date() - timedelta(days=DAILY_KEEP_DAYS)
        weeks: dict[str, list[tuple[date, Path, Any]]] = {}
        for row in self.memory.index.list_notes("daily"):
            if not _DAILY_TITLE.match(row["title"]):
                continue
            day = date.fromisoformat(row["title"])
            if day >= cutoff:
                continue
            iso = day.isocalendar()
            weeks.setdefault(f"{iso.year}-W{iso.week:02d}", []).append((day, self.memory.vault.root / row["path"], row))
        compressed = 0
        for week, days in sorted(weeks.items()):
            days.sort(key=lambda x: x[0])
            lines: list[tuple[str, str]] = []
            for day, path, _ in days:
                for b in self.memory.vault.read(path).parsed().blocks:
                    lines.append((day.isoformat(), b.text))
            self._record("compress_daily", week=week, days=[d.isoformat() for d, _, _ in days], lines=len(lines))
            compressed += len(days)
            if self._dry:
                continue
            row = next((r for r in self.memory.index.list_notes("daily") if r["title"] == week), None)
            path = (self.memory.vault.root / row["path"]) if row else self.memory.vault.create("daily", week).path
            for day_s, text in lines:
                self.memory.vault.mutate(path, lambda n, day_s=day_s, text=text: Vault.add_fact(n, f"{day_s}: {text}", f"consolidation · daily/{day_s}"))
            kept = {parser.normalize(parser.strip_links(b.text)) for b in self.memory.vault.read(path).parsed().blocks}
            if not all(parser.normalize(parser.strip_links(f"{d}: {t}")) in kept for d, t in lines):
                self._record("error", step="compress_daily", week=week, reason="weekly note is missing lines; originals kept")
                continue
            for _, p, _ in days:
                self._trash(str(p))                      # Recycle Bin: restorable
        return compressed

    # ---- 4. importance ------------------------------------------------------------------------------------------------
    def decay_importance(self) -> int:
        now = self._clock()
        own = self.state.setdefault("own_updates", {})
        decays = self.state.setdefault("decay", {})
        with self.memory.index.lock:
            used = {r["note_id"]: r["last_used"] for r in self.memory.index.db.execute("SELECT note_id, last_used FROM access")}
        decayed = 0
        for row in self.memory.index.list_notes():
            if row["type"] in ("daily", "inbox") or row["type"] in SINGLETONS or row["importance"] <= 1:
                continue
            activity = [row["created"]]
            if row["updated"] and own.get(row["id"]) != row["updated"]:
                activity.append(row["updated"])          # edited by someone else since we last touched it
            if used.get(row["id"]):
                activity.append(used[row["id"]])
            idle = min(_days_since(a, now) for a in activity if a)
            since_decay = _days_since(decays[row["id"]], now) if row["id"] in decays else 1e9
            if idle < DECAY_AFTER_DAYS or since_decay < DECAY_EVERY_DAYS:
                continue
            self._record("decay_importance", note=row["title"], path=row["path"], importance_from=row["importance"],
                         importance_to=row["importance"] - 1, idle_days=round(idle))
            decayed += 1
            if self._dry:
                continue
            path = self.memory.vault.root / row["path"]

            def apply(n):
                n.importance = max(1, n.importance - 1)

            note = self.memory.vault.mutate(path, apply)
            own[row["id"]] = note.updated
            decays[row["id"]] = now.isoformat(timespec="seconds")
        if not self._dry:
            self._save()
        return decayed
