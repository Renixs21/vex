"""Reminders: APScheduler in-process, persisted to data/reminders.json, shown as a Windows toast."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

import dateparser

from memory.secrets import find_secrets
from memory.vault import atomic_write
from tools.registry import ToolResult, tool

log = logging.getLogger("vex.reminders")
_scheduler = None
_lock = threading.RLock()


def data_path() -> Path:
    from config import ROOT

    return ROOT / "data" / "reminders.json"


def _load() -> list[dict]:
    try:
        return json.loads(data_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    atomic_write(data_path(), json.dumps(items, ensure_ascii=False, indent=1))


def notify(title: str, message: str) -> None:
    """Show a Windows toast (patched in tests)."""
    from winotify import Notification

    Notification(app_id="Vex", title=title, msg=message, duration="long").show()


def _fire(rid: str, missed: bool = False) -> None:
    with _lock:
        items = _load()
        item = next((i for i in items if i["id"] == rid and not i.get("done")), None)
        if item is None:
            return
        item["done"] = True
        _save(items)
    try:
        notify("Reminder" + (" (missed)" if missed else ""), item["text"])
    except Exception:  # noqa: BLE001 - a failed toast must not kill the scheduler thread
        log.exception("could not show reminder")


def start() -> None:
    """Start the scheduler and re-schedule saved reminders. Reminders that came due while Vex was off fire now."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    with _lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.start()
        for item in _load():
            if item.get("done"):
                continue
            due = datetime.fromisoformat(item["due"])
            if due <= datetime.now():
                threading.Thread(target=_fire, args=(item["id"], True), daemon=True).start()
            else:
                _scheduler.add_job(_fire, "date", run_date=due, args=[item["id"]], id=item["id"], replace_existing=True)


def stop() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


@tool("set_reminder", "Set a reminder, e.g. 'in 20 minutes' or 'tomorrow 5pm'.",
      {"text": {"type": "string", "description": "What to remind the user about"},
       "when": {"type": "string", "description": "When, in natural language"}},
      describe=lambda a: f"Set reminder '{a['text']}' for {a['when']}")
def set_reminder(text: str, when: str) -> ToolResult:
    text = " ".join(text.split())
    if not text:
        return ToolResult(False, "The reminder is empty.")
    if find_secrets(text):
        return ToolResult(False, "Not saving that: it looks like it contains a secret.")
    due = dateparser.parse(when, settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False})
    if due is None:
        return ToolResult(False, f"I couldn't understand the time '{when}'.")
    if due <= datetime.now():
        return ToolResult(False, f"{due:%A %d %B, %H:%M} has already passed.")
    start()
    rid = uuid.uuid4().hex[:8]
    with _lock:
        _save([*_load(), {"id": rid, "text": text, "due": due.isoformat(timespec="seconds"), "done": False}])
        _scheduler.add_job(_fire, "date", run_date=due, args=[rid], id=rid, replace_existing=True)
    return ToolResult(True, f"I'll remind you on {due:%A %d %B at %H:%M}: {text}", {"id": rid, "due": due.isoformat()})
