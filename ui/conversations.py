"""Conversation history for the Control Center: sessions (one wake-up and its follow-ups), each with its turns (what you said, Vex's
reply), the tools each turn used, and its timings.

Privacy: kept in memory for this run, and on disk in data/conversations.jsonl (gitignored) for [api] keep_conversations_days (14 by
default; 0 = never written to disk). Secrets (passwords, keys, card numbers...) are redacted before anything is stored. Older sessions
are dropped when Vex starts and once a day.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("vex.conversations")

MAX_SESSIONS = 200
MAX_TEXT = 4000


def _now() -> datetime:
    return datetime.now().astimezone()


def _clean(text: str) -> str:
    from memory.secrets import redact

    return redact(str(text or ""))[:MAX_TEXT]


class ConversationLog:
    def __init__(self, path: Path | None, keep_days: int = 14, clock=_now):
        self.path = path if keep_days > 0 else None
        self.keep_days = keep_days
        self._clock = clock
        self._lock = threading.Lock()
        self.sessions: deque[dict] = deque(maxlen=MAX_SESSIONS)
        self._current: dict | None = None
        self._pending_tools: list[dict] = []
        self._last_prune = clock()
        self._load()

    # ---- disk -----------------------------------------------------------------------------------------------------------------------
    def _cutoff(self) -> str:
        return (self._clock() - timedelta(days=self.keep_days)).isoformat(timespec="seconds")

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        cutoff, kept = self._cutoff(), []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if str(s.get("start", "")) >= cutoff:
                    kept.append(s)
        except OSError:
            return
        self.sessions.extend(kept[-MAX_SESSIONS:])
        if len(kept) < sum(1 for _ in self.path.read_text(encoding="utf-8").splitlines()):
            self._rewrite(kept)

    def _rewrite(self, sessions: list[dict]) -> None:
        if self.path is None:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sessions), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not rewrite the conversation history", exc_info=True)

    def prune(self) -> int:
        """Drop sessions older than keep_days (memory and disk). Returns how many went."""
        cutoff = self._cutoff()
        with self._lock:
            before = len(self.sessions)
            keep = [s for s in self.sessions if str(s.get("start", "")) >= cutoff]
            self.sessions.clear()
            self.sessions.extend(keep)
        self._rewrite(keep)
        return before - len(keep)

    # ---- recording (hooked to the controller, the executor and the latency timeline) -------------------------------------------------------
    def start_session(self) -> None:
        with self._lock:
            self._current = {"id": uuid.uuid4().hex[:12], "start": self._clock().isoformat(timespec="seconds"), "end": None, "turns": []}
            self._pending_tools = []

    def add_tool(self, event: dict) -> None:
        if event.get("event") != "tool_end":
            return
        with self._lock:
            self._pending_tools.append({"tool": event.get("tool"), "tier": event.get("tier"), "decision": event.get("decision"),
                                        "ok": bool(event.get("ok"))})

    def add_turn(self, said: str, reply: str) -> None:
        with self._lock:
            if self._current is None:                          # a turn outside an interaction (shouldn't happen): its own session
                self._current = {"id": uuid.uuid4().hex[:12], "start": self._clock().isoformat(timespec="seconds"), "end": None, "turns": []}
            self._current["turns"].append({"ts": self._clock().isoformat(timespec="seconds"), "said": _clean(said), "reply": _clean(reply),
                                           "tools": self._pending_tools, "latency": None})
            self._pending_tools = []

    def add_timing(self, rec: dict) -> None:
        """The latency record of the turn that just finished: attached to the newest turn that has none yet."""
        if rec.get("outcome") != "spoken":
            return
        with self._lock:
            if self._current and self._current["turns"] and self._current["turns"][-1]["latency"] is None:
                st = rec.get("stages", {})
                self._current["turns"][-1]["latency"] = {k: st[k] for k in ("total", "stt", "brain", "tts_first_audio") if k in st}
                self._current["turns"][-1]["via"] = rec.get("source")

    def end_session(self) -> dict | None:
        with self._lock:
            s, self._current = self._current, None
            self._pending_tools = []
        if not s or not s["turns"]:
            return None
        s["end"] = self._clock().isoformat(timespec="seconds")
        with self._lock:
            self.sessions.append(s)
        if (self._clock() - self._last_prune) > timedelta(days=1):    # once a day: drop what is past keep_days
            self._last_prune = self._clock()
            self.prune()
            return s
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            except OSError:
                log.warning("could not save the conversation", exc_info=True)
        return s

    # ---- reading ---------------------------------------------------------------------------------------------------------------------------
    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self.sessions, key=lambda s: str(s.get("start", "")), reverse=True)[:limit]
        out = []
        for s in items:
            totals = [t["latency"]["total"] for t in s["turns"] if t.get("latency") and "total" in t["latency"]]
            out.append({"id": s["id"], "start": s["start"], "end": s["end"], "turns": len(s["turns"]),
                        "first": s["turns"][0]["said"][:120] if s["turns"] else "",
                        "tools": sorted({x["tool"] for t in s["turns"] for x in t.get("tools", []) if x.get("tool")}),
                        "median_total": sorted(totals)[len(totals) // 2] if totals else None})
        return out

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            return next((dict(s) for s in self.sessions if s["id"] == session_id), None)
