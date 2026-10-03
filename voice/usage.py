"""Usage tracking for the services with free allowances: Whisper audio seconds and Google TTS characters.

Persisted in data/voice_usage.json so limits survive restarts. Only counts and timestamps are stored: never audio or text.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from memory.vault import atomic_write

DAY = 86400.0


class UsageStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("months", {})
        data.setdefault("events", {})
        return data

    def _save(self, data: dict) -> None:
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    # ---- monthly counters (Google TTS characters) --------------------------------------------------------------------
    def _month(self) -> str:
        return datetime.fromtimestamp(self._clock()).strftime("%Y-%m")

    def add_month(self, key: str, amount: float) -> float:
        """Add to this calendar month's counter and return the new total. A new month starts from zero by itself."""
        with self._lock:
            data = self._load()
            month = data["months"].setdefault(self._month(), {})
            month[key] = month.get(key, 0) + amount
            data["months"] = {m: v for m, v in data["months"].items() if m >= self._month()[:4] + "-01"}   # keep this year
            self._save(data)
            return month[key]

    def month_total(self, key: str) -> float:
        return self._load()["months"].get(self._month(), {}).get(key, 0)

    # ---- rolling windows (Whisper seconds per hour) --------------------------------------------------------------------
    def add_event(self, key: str, amount: float) -> None:
        with self._lock:
            data = self._load()
            now = self._clock()
            events = [e for e in data["events"].get(key, []) if now - e[0] < DAY]
            events.append([now, amount])
            data["events"][key] = events
            self._save(data)

    def window_total(self, key: str, window: float) -> float:
        now = self._clock()
        return sum(a for ts, a in self._load()["events"].get(key, []) if now - ts < window)

    def seconds_until_room(self, key: str, window: float, limit: float, needed: float) -> float:
        """How long until `needed` more units fit under `limit` in the rolling window (0 if they fit now)."""
        now = self._clock()
        events = sorted((ts, a) for ts, a in self._load()["events"].get(key, []) if now - ts < window)
        used = sum(a for _ts, a in events)
        if used + needed <= limit:
            return 0.0
        for ts, a in events:                      # oldest first: wait until enough of them age out of the window
            used -= a
            if used + needed <= limit:
                return max(0.0, ts + window - now)
        return window
