"""How much of each model's free allowance Vex uses: requests and tokens per model per day, in data/llm_usage.json (40 days kept).

Every successful model call counts (live replies, the memory curator, the nightly tidy-up), because they all spend the same daily
quota. Only counts are stored: no prompts, no replies. The Control Center's Budget screen reads it through the local API.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("vex.usage")

KEEP_DAYS = 40


class LlmUsage:
    def __init__(self, path: Path, today: Callable[[], date] = lambda: datetime.now().date()):
        self.path = path
        self._today = today
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict]] = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=0), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.debug("could not save model usage", exc_info=True)

    def record(self, provider: str, model: str, prompt: int | None, completion: int | None) -> None:
        day = self._today().isoformat()
        with self._lock:
            entry = self._data.setdefault(day, {}).setdefault(provider, {"model": model, "requests": 0, "prompt": 0, "completion": 0})
            entry["model"] = model
            entry["requests"] += 1
            entry["prompt"] += int(prompt or 0)
            entry["completion"] += int(completion or 0)
            cutoff = (self._today() - timedelta(days=KEEP_DAYS)).isoformat()
            for d in [d for d in self._data if d < cutoff]:
                del self._data[d]
            self._save()

    def summary(self, days: int = 7) -> dict:
        """{days: [iso dates oldest first], models: [{provider, model, today: {...}, totals: [tokens per day]}]}."""
        today = self._today()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        with self._lock:
            providers = sorted({p for d in self._data.values() for p in d})
            out = []
            for p in providers:
                per_day = [self._data.get(d, {}).get(p) for d in dates]
                t = per_day[-1] or {"requests": 0, "prompt": 0, "completion": 0}
                model = next((e["model"] for e in reversed(per_day) if e), "") or next(
                    (d[p]["model"] for d in self._data.values() if p in d), "")
                out.append({"provider": p, "model": model,
                            "today": {"requests": t["requests"], "prompt": t["prompt"], "completion": t["completion"],
                                      "tokens": t["prompt"] + t["completion"]},
                            "totals": [(e["prompt"] + e["completion"]) if e else 0 for e in per_day],
                            "requests": [e["requests"] if e else 0 for e in per_day]})
        return {"days": dates, "models": out}


_recorder: LlmUsage | None = None


def set_recorder(rec: LlmUsage | None) -> None:
    """The app installs one at startup; without it (tests, text mode) nothing is recorded."""
    global _recorder
    _recorder = rec


def record(provider: str, model: str, prompt: int | None, completion: int | None) -> None:
    rec = _recorder
    if rec is not None:
        try:
            rec.record(provider, model, prompt, completion)
        except Exception:  # noqa: BLE001 - counting must never break a reply
            log.debug("usage record failed", exc_info=True)
