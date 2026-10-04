"""Housekeeping: keep Vex's own files from growing forever. Runs 2 minutes after start, then once a day.

    python tools_dev/housekeeping.py            # run it now
    python tools_dev/housekeeping.py --dry-run  # only say what it would do

What it does (and nothing else):
  * logs/actions.jsonl (the audit log) at 5 MB -> appended to logs/archive/actions-YYYY-MM.jsonl.gz; those archives are kept to 50 MB
    in total (oldest month dropped first).
  * logs/curator.jsonl, memory_consolidation.jsonl, latency.jsonl at 2 MB -> name.1.jsonl.gz, .2, .3 (3 kept).
  * Stray files in logs/ older than 30 days: console*.txt, app_stdout*.txt, bench_*.json, eval_*.json, *.old.txt. Leftover *.tmp files
    older than 30 days in logs/, data/ and the vault's hidden .vexmind folder.
  * data/undo_backups (copies kept for "undo that") capped at 50 MB, oldest first.
  * The vault (VexVault): a 200 MB SOFT cap. At 80% you get a warning (once a day); at 100% automatic memory entries (the curator)
    pause until there is room again. Notes are NEVER deleted, moved or edited by housekeeping: no file in the vault except stray .tmp
    leftovers inside .vexmind is ever touched.

Every archive, rotation and removal is written to logs/housekeeping.log in plain words.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("vex.housekeeping")

MB = 1_000_000
ACTIONS_ROTATE_MB = 5.0
ACTIONS_ARCHIVE_CAP_MB = 50.0
JSONL_ROTATE_MB = 2.0
JSONL_KEEP = 3
ROTATED_LOGS = ("curator.jsonl", "memory_consolidation.jsonl", "latency.jsonl")
STRAY_PATTERNS = ("console*.txt", "app_stdout*.txt", "bench_*.json", "eval_*.json", "*.old.txt")
STRAY_DAYS = 30
UNDO_CAP_MB = 50.0
VAULT_CAP_MB = 200.0
VAULT_WARN_PCT = 80.0
HK_LOG_MAX = 500_000


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mb(n: float) -> str:
    return f"{n / MB:.1f} MB"


class Housekeeper:
    def __init__(self, root: Path, logs: Path | None = None, vault: Path | None = None, data: Path | None = None,
                 notify: Callable[[str, str], None] | None = None, clock: Callable[[], float] = time.time,
                 vault_cap_mb: float = VAULT_CAP_MB):
        self.root = Path(root)
        self.logs = Path(logs) if logs else self.root / "logs"
        self.vault = Path(vault) if vault else self.root / "VexVault"
        self.data = Path(data) if data else self.root / "data"
        self.notify = notify
        self._clock = clock
        self.vault_cap_mb = vault_cap_mb
        self._vault_cache: tuple[float, float] | None = None       # (when, bytes)
        self._warned_day = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the log ---------------------------------------------------------------------------------------------------------------
    def _note(self, actions: list[str], text: str, dry_run: bool) -> None:
        line = ("WOULD " if dry_run else "") + text
        actions.append(line)
        log.info("housekeeping: %s", line)
        if dry_run:
            return
        path = self.logs / "housekeeping.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _size(path) > HK_LOG_MAX:                          # keep the newest half
                lines = path.read_text(encoding="utf-8").splitlines()
                path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.fromtimestamp(self._clock()):%Y-%m-%d %H:%M}  {line}\n")
        except OSError:
            log.warning("could not write the housekeeping log", exc_info=True)

    # ---- the steps -----------------------------------------------------------------------------------------------------------------
    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    @staticmethod
    def _gzip_append(src: Path, dest: Path) -> None:
        """Append src to dest.gz as a new gzip member (several members read back as one stream)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fin, gzip.open(dest, "ab") as fout:
            shutil.copyfileobj(fin, fout)

    def _take(self, path: Path) -> Path | None:
        """Move a live log aside so writers start a fresh file at once. None if it is busy (tried again next run)."""
        tmp = path.with_name(path.name + ".rotating")
        try:
            path.replace(tmp)
            return tmp
        except OSError:
            log.info("housekeeping: %s is busy; will rotate it next time", path.name)
            return None

    def rotate_actions(self, actions: list[str], dry_run: bool) -> None:
        path = self.logs / "actions.jsonl"
        size = _size(path)
        if size >= ACTIONS_ROTATE_MB * MB:
            dest = self.logs / "archive" / f"actions-{datetime.fromtimestamp(self._clock()):%Y-%m}.jsonl.gz"
            if not dry_run:
                tmp = self._take(path)
                if tmp is None:
                    return
                self._gzip_append(tmp, dest)
                tmp.unlink()
            self._note(actions, f"ARCHIVED {self._rel(path)} ({_mb(size)}) -> {self._rel(dest)}", dry_run)
        archives = sorted((self.logs / "archive").glob("actions-*.jsonl.gz"))       # names sort by month
        total = sum(_size(a) for a in archives)
        while archives and total > ACTIONS_ARCHIVE_CAP_MB * MB and len(archives) > 1:
            old = archives.pop(0)
            total -= _size(old)
            if not dry_run:
                old.unlink(missing_ok=True)
            self._note(actions, f"REMOVED old audit archive {self._rel(old)} (archives over {ACTIONS_ARCHIVE_CAP_MB:.0f} MB)", dry_run)

    def rotate_jsonl(self, actions: list[str], dry_run: bool) -> None:
        for name in ROTATED_LOGS:
            path = self.logs / name
            size = _size(path)
            if size < JSONL_ROTATE_MB * MB:
                continue
            stem = name[:-len(".jsonl")]
            gen = lambda i: self.logs / f"{stem}.{i}.jsonl.gz"  # noqa: E731
            if not dry_run:
                tmp = self._take(path)
                if tmp is None:
                    continue
                gen(JSONL_KEEP).unlink(missing_ok=True)
                for i in range(JSONL_KEEP - 1, 0, -1):
                    if gen(i).exists():
                        gen(i).replace(gen(i + 1))
                with tmp.open("rb") as fin, gzip.open(gen(1), "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                tmp.unlink()
            self._note(actions, f"ROTATED {self._rel(path)} ({_mb(size)}) -> {gen(1).name} (keeping {JSONL_KEEP})", dry_run)

    def remove_strays(self, actions: list[str], dry_run: bool) -> None:
        cutoff = self._clock() - STRAY_DAYS * 86400
        found: dict[Path, None] = {}
        for pat in STRAY_PATTERNS:
            for p in self.logs.glob(pat):
                found[p] = None
        for folder in (self.logs, self.data, self.vault / ".vexmind"):
            for p in folder.glob("*.tmp"):
                found[p] = None
        for p in sorted(found):
            try:
                if not p.is_file() or p.stat().st_mtime >= cutoff:
                    continue
                size = _size(p)
                if not dry_run:
                    p.unlink()
                self._note(actions, f"REMOVED {self._rel(p)} ({_mb(size)}, untouched for over {STRAY_DAYS} days)", dry_run)
            except OSError as e:
                log.info("housekeeping: couldn't remove %s: %s", p, e)

    def cap_undo_backups(self, actions: list[str], dry_run: bool) -> None:
        folder = self.data / "undo_backups"
        files = sorted((p for p in folder.glob("*.bak") if p.is_file()), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
        total = sum(_size(p) for p in files)
        removed, freed = 0, 0
        while files and total > UNDO_CAP_MB * MB:
            p = files.pop(0)
            n = _size(p)
            total -= n
            if not dry_run:
                p.unlink(missing_ok=True)
            removed, freed = removed + 1, freed + n
        if removed:
            self._note(actions, f"REMOVED {removed} oldest undo backup(s) ({_mb(freed)}; undo backups capped at {UNDO_CAP_MB:.0f} MB; "
                                "those old edits can no longer be undone)", dry_run)

    # ---- the vault's soft cap ---------------------------------------------------------------------------------------------------------
    def vault_bytes(self, max_age: float = 600.0) -> float:
        now = self._clock()
        if self._vault_cache is None or now - self._vault_cache[0] > max_age:
            total = 0
            if self.vault.is_dir():
                for p in self.vault.rglob("*"):
                    try:
                        if p.is_file():
                            total += p.stat().st_size
                    except OSError:
                        pass
            self._vault_cache = (now, float(total))
        return self._vault_cache[1]

    def vault_percent(self) -> float:
        return self.vault_bytes() / (self.vault_cap_mb * MB) * 100

    def vault_full(self) -> bool:
        """True at or over the soft cap: automatic memory entries pause. Your own 'remember ...' still works; nothing is deleted."""
        return self.vault_percent() >= 100.0

    def check_vault(self, actions: list[str], dry_run: bool) -> None:
        self._vault_cache = None
        pct, used = self.vault_percent(), self.vault_bytes()
        day = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")
        if pct < VAULT_WARN_PCT:
            return
        full = pct >= 100
        msg = (f"Your memory vault is {_mb(used)}, {pct:.0f}% of its {self.vault_cap_mb:.0f} MB soft cap. "
               + ("Automatic memory entries are paused until there's room. " if full else "")
               + "Nothing is deleted automatically: open the vault (tray > Open memory) to tidy it, or raise the cap.")
        self._note(actions, f"VAULT {pct:.0f}% of the soft cap ({_mb(used)})" + (": automatic entries paused" if full else ""), dry_run)
        if not dry_run and self.notify and self._warned_day != day:
            self._warned_day = day
            try:
                self.notify("Vex memory is getting big" if not full else "Vex memory is full", msg)
            except Exception:  # noqa: BLE001
                log.debug("notify failed", exc_info=True)

    # ---- running ------------------------------------------------------------------------------------------------------------------------
    def run(self, dry_run: bool = False) -> list[str]:
        """One pass. Returns what was done (or would be done), one plain sentence per item. Never raises."""
        actions: list[str] = []
        for step in (self.rotate_actions, self.rotate_jsonl, self.remove_strays, self.cap_undo_backups, self.check_vault):
            try:
                step(actions, dry_run)
            except Exception:  # noqa: BLE001 - housekeeping must never take Vex down
                log.exception("housekeeping step %s failed", step.__name__)
        log.info("housekeeping done: %d action(s)", len(actions))
        return actions

    def start(self, first_delay: float = 120.0, interval: float = 86400.0) -> None:
        def loop() -> None:
            if self._stop.wait(first_delay):
                return
            while True:
                self.run()
                if self._stop.wait(interval):
                    return

        self._thread = threading.Thread(target=loop, name="housekeeping", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python tools_dev/housekeeping.py", description="Trim Vex's logs and backups (never your notes).")
    ap.add_argument("--dry-run", action="store_true", help="only say what would be done")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    import config

    hk = Housekeeper(config.ROOT, config.ROOT / config.settings["paths"]["logs"], config.ROOT / config.settings["paths"]["vault"])
    done = hk.run(dry_run=a.dry_run)
    print("\n".join(done) if done else "Nothing to do: every log, archive and backup is within its limits.")
    print(f"Vault: {hk.vault_bytes() / MB:.1f} MB of the {hk.vault_cap_mb:.0f} MB soft cap ({hk.vault_percent():.0f}%).")
    if not a.dry_run and done:
        print(f"(logged in {hk.logs / 'housekeeping.log'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
