"""Append-only audit log: one JSON line per tool call in logs/actions.jsonl.

Records time, tool, arguments (secrets redacted; omitted for memory and clipboard tools), tier, decision
(auto | approved | denied | timeout | blocked | refused | dry_run | unknown_tool) and a short result summary.
The file is only ever opened in append mode, and file tools are blocked from writing to it.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.secrets import redact


def _known_secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if k.endswith(("_API_KEY", "_TOKEN", "_SECRET")) and v]


def scrub(value: Any) -> Any:
    """Redact secrets inside any JSON-like value."""
    known = _known_secret_values()
    if isinstance(value, str):
        return redact(value, known)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, *, tool: str, tier: str, decision: str, ok: bool | None = None, args: dict | None = None,
               summary: str | None = None, reason: str | None = None, undo: bool = False, **extra: Any) -> str:
        """Append one entry and return its id."""
        entry_id = uuid.uuid4().hex[:10]
        entry: dict[str, Any] = {"id": entry_id, "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                 "tool": tool, "tier": tier, "decision": decision}
        if args is not None:
            entry["args"] = scrub(args)
        if ok is not None:
            entry["ok"] = ok
        if summary:
            entry["result"] = scrub(summary)[:300]
        if reason:
            entry["reason"] = scrub(reason)[:200]
        if undo:
            entry["undoable"] = True
        entry.update({k: scrub(v) for k, v in extra.items() if v is not None})
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry_id

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(l) for l in lines if l.strip()]
