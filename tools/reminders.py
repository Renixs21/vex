"""Reminders: one-time and recurring ("every weekday at 9"), spoken aloud with a chime, persisted in data/reminders.json.

When one fires Vex says it out loud (through the announcer the app installs: chime, orb, voice, waiting its turn if Vex is busy) and also
shows a quiet toast. Without an announcer (text mode) the toast is the notification. Reminders survive restarts: one-time ones that came due
while Vex was off fire at the next start ("missed"); a recurring one that missed an occurrence announces it once. APScheduler runs them
in-process, and a sweep every minute catches anything a sleeping PC skipped.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import dateparser
from rapidfuzz import fuzz

from memory.secrets import find_secrets
from memory.vault import atomic_write
from safety.policy import Decision
from tools.registry import YELLOW, ToolResult, tool

log = logging.getLogger("vex.reminders")
_scheduler = None
_lock = threading.RLock()
_announcer: Callable[[str], object] | None = None
MAX_REMINDERS = 200
KEEP_DONE_DAYS = 14
SWEEP_SECONDS = 60
MIN_INTERVAL_MINUTES = 5

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_WORDS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
              "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def data_path() -> Path:
    from config import ROOT

    return ROOT / "data" / "reminders.json"


def _load() -> list[dict]:
    try:
        return json.loads(data_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save(items: list[dict]) -> None:
    cutoff = (datetime.now() - timedelta(days=KEEP_DONE_DAYS)).isoformat(timespec="seconds")
    kept = [i for i in items if not (i.get("done") and i.get("done_at", i.get("due", "")) < cutoff)]
    atomic_write(data_path(), json.dumps(kept, ensure_ascii=False, indent=1))


# ---- delivering ---------------------------------------------------------------------------------------------------------------
def notify(title: str, message: str) -> None:
    """Show a Windows toast (patched in tests)."""
    from winotify import Notification

    Notification(app_id="Vex", title=title, msg=message, duration="long").show()


def notify_silent(title: str, message: str) -> None:
    """A toast without its own sound: Vex is already speaking and chiming."""
    from winotify import Notification, audio

    n = Notification(app_id="Vex", title=title, msg=message, duration="long")
    n.set_audio(audio.Silent, loop=False)
    n.show()


def set_announcer(fn: Callable[[str], object] | None) -> None:
    """The app installs a function that speaks a sentence (with a chime) when a reminder fires."""
    global _announcer
    _announcer = fn


def _deliver(title: str, text: str, spoken: str) -> None:
    spoke = False
    if _announcer is not None:
        try:
            _announcer(spoken)
            spoke = True
        except Exception:  # noqa: BLE001
            log.exception("could not announce the reminder; falling back to the toast")
    try:
        (notify_silent if spoke else notify)(title, text)
    except Exception:  # noqa: BLE001 - a failed toast must not kill the scheduler thread
        log.exception("could not show reminder")


# ---- time and recurrence parsing --------------------------------------------------------------------------------------------------
_TIME_WORDS = {"noon": (12, 0), "midnight": (0, 0), "morning": (9, 0), "afternoon": (15, 0), "evening": (18, 0), "night": (21, 0)}


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    """'9', '9am', '9:30 pm', 'noon', '18:45', 'at 7' -> (hour, minute). A bare 7-11 means morning, 1-6 afternoon."""
    t = text.lower()
    if m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a|p)\.?m\b\.?", t):
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        return (h + (12 if m.group(3) == "p" else 0), mi) if h <= 12 and mi < 60 else None
    if m := re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t):
        return int(m.group(1)), int(m.group(2))
    if m := re.search(r"\bat\s+(\d{1,2})\b", t):
        h = int(m.group(1))
        if h > 23:
            return None
        return ((h + 12 if 1 <= h <= 6 else h), 0)
    for word, hm in _TIME_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            return hm
    return None


def _clock_text(h: int, m: int) -> str:
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


_RECURRING = re.compile(r"\b(every|each|daily|weekly|monthly|hourly|nightly|weekdays?|weekends?|everyday)\b", re.I)


def is_recurring(when: str) -> bool:
    return bool(_RECURRING.search(when or ""))


def parse_recurrence(when: str) -> dict:
    """'every weekday at 9' -> a rule dict. Raises ValueError with a question to ask the user when something is missing."""
    t = " ".join(when.lower().replace(",", " ").split())
    hm = parse_time_of_day(t)
    if m := re.search(r"every\s+(\d+|other)\s*(minute|min|hour|hr|day)s?\b", t):
        n = 2 if m.group(1) == "other" else int(m.group(1))
        unit = m.group(2)
        if n < 1:
            raise ValueError("How often should that repeat?")
        if unit.startswith(("min", "hour", "hr")):
            minutes = n * (1 if unit.startswith("min") else 60)
            if minutes < MIN_INTERVAL_MINUTES:
                raise ValueError(f"I won't repeat more often than every {MIN_INTERVAL_MINUTES} minutes.")
            return {"freq": "interval", "minutes": minutes}
        return {"freq": "days", "every": n, "time": "%02d:%02d" % (hm or (9, 0))}
    if re.search(r"every\s+(hour|minute)\b|\bhourly\b", t):
        return {"freq": "interval", "minutes": 60 if re.search(r"hour", t) else MIN_INTERVAL_MINUTES}
    if m := re.search(r"(?:every\s+month|monthly)(?:\s+on)?(?:\s+the)?\s*(\d{1,2})(?:st|nd|rd|th)?\b", t) or \
            re.search(r"on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+of\s+(?:every|each)\s+month", t):
        day = int(m.group(1))
        if not 1 <= day <= 28:
            raise ValueError("Monthly reminders work for days 1 to 28. Which day should I use?")
        return {"freq": "monthly", "day": day, "time": "%02d:%02d" % (hm or (9, 0))}
    if re.search(r"monthly|every\s+month", t):
        raise ValueError("Which day of the month should it repeat on?")
    if "weekday" in t:
        days = [0, 1, 2, 3, 4]
    elif "weekend" in t:
        days = [5, 6]
    else:
        named = sorted({v for k, v in _DAY_WORDS.items() if re.search(rf"\b{k}s?\b", t)})
        if named:
            days = named
        elif re.search(r"\b(every\s*day|daily|each\s+day|everyday|nightly)\b", t):
            days = list(range(7))
        else:
            raise ValueError("Which days should it repeat on? For example 'every weekday at 9' or 'every Monday at 7pm'.")
    if hm is None:
        raise ValueError("What time should it repeat at? For example 'every weekday at 9am'.")
    return {"freq": "weekly", "days": days, "time": "%02d:%02d" % hm}


def describe_rule(rule: dict) -> str:
    if rule["freq"] == "interval":
        m = rule["minutes"]
        return f"every {m // 60} hour{'s' if m // 60 != 1 else ''}" if m % 60 == 0 and m >= 60 else f"every {m} minutes"
    h, mi = (int(x) for x in rule.get("time", "09:00").split(":"))
    at = _clock_text(h, mi)
    if rule["freq"] == "days":
        return f"every {rule['every']} days at {at}"
    if rule["freq"] == "monthly":
        d = rule["day"]
        suffix = "th" if 10 <= d % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        return f"on the {d}{suffix} of every month at {at}"
    days = rule["days"]
    if days == list(range(7)):
        which = "every day"
    elif days == [0, 1, 2, 3, 4]:
        which = "every weekday"
    elif days == [5, 6]:
        which = "every weekend"
    else:
        names = [_DAY_FULL[d] for d in days]
        which = "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    return f"{which} at {at}"


def _tz():
    from tzlocal import get_localzone

    return get_localzone()


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=_tz()) if dt.tzinfo is None else dt


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(_tz()).replace(tzinfo=None)


def trigger_for(rule: dict, created: datetime):
    """The APScheduler trigger for a rule (also used to compute the next and the missed occurrences)."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = _tz()
    if rule["freq"] == "interval":
        step = timedelta(minutes=rule["minutes"])
        return IntervalTrigger(minutes=rule["minutes"], start_date=_aware(created + step), timezone=tz)       # first one a full step from now
    h, mi = (int(x) for x in rule["time"].split(":"))
    if rule["freq"] == "days":
        start = created.replace(hour=h, minute=mi, second=0, microsecond=0)
        if start <= created:
            start += timedelta(days=1)
        return IntervalTrigger(days=rule["every"], start_date=_aware(start), timezone=tz)
    if rule["freq"] == "monthly":
        return CronTrigger(day=rule["day"], hour=h, minute=mi, timezone=tz)
    return CronTrigger(day_of_week=",".join(DAY_NAMES[d] for d in rule["days"]), hour=h, minute=mi, timezone=tz)


def next_occurrence(rule: dict, created: datetime, after: datetime | None = None) -> datetime | None:
    after = after or datetime.now()
    nxt = trigger_for(rule, created).get_next_fire_time(None, _aware(after))
    return _naive(nxt) if nxt else None


# ---- scheduling and firing -----------------------------------------------------------------------------------------------------------
def _schedule(item: dict) -> None:
    if _scheduler is None or item.get("done"):
        return
    rid = item["id"]
    if item.get("repeat"):
        trig = trigger_for(item["repeat"], datetime.fromisoformat(item.get("created") or item["due"]))
        _scheduler.add_job(_fire, trig, args=[rid], id=rid, replace_existing=True, misfire_grace_time=3600, coalesce=True)
    else:
        _scheduler.add_job(_fire, "date", run_date=_aware(datetime.fromisoformat(item["due"])), args=[rid], id=rid,
                           replace_existing=True, misfire_grace_time=3600)


def _unschedule(rid: str) -> None:
    if _scheduler is not None:
        try:
            _scheduler.remove_job(rid)
        except Exception:  # noqa: BLE001 - already gone
            pass


def _fire(rid: str, missed: bool = False) -> None:
    now = datetime.now()
    with _lock:
        items = _load()
        item = next((i for i in items if i["id"] == rid and not i.get("done")), None)
        if item is None:
            return
        if item.get("repeat"):
            if not missed and datetime.fromisoformat(item["due"]) > now + timedelta(seconds=5):
                return                                           # the sweep or the job already handled this occurrence
            item["last_fired"] = now.isoformat(timespec="seconds")
            nxt = next_occurrence(item["repeat"], datetime.fromisoformat(item.get("created") or item["due"]), now)
            item["due"] = (nxt or now).isoformat(timespec="seconds")
        else:
            item["done"] = True
            item["done_at"] = now.isoformat(timespec="seconds")
        _save(items)
    text = item["text"]
    _deliver("Reminder" + (" (missed)" if missed else ""), text, f"Reminder{' from earlier' if missed else ''}: {text}")


def _sweep() -> None:
    """Fire anything overdue that the scheduler skipped (a sleeping PC): once, announced as missed if it is well late."""
    now = datetime.now()
    for item in _load():
        if item.get("done"):
            continue
        due = datetime.fromisoformat(item["due"])
        if due <= now:
            _fire(item["id"], missed=(now - due).total_seconds() > 150)


def start() -> None:
    """Start the scheduler and re-schedule saved reminders. Reminders that came due while Vex was off fire now."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    with _lock:
        if _scheduler is not None:
            return
        _scheduler = BackgroundScheduler(daemon=True, timezone=_tz())
        _scheduler.start()
        now = datetime.now()
        for item in _load():
            if item.get("done"):
                continue
            if item.get("repeat"):
                created = datetime.fromisoformat(item.get("created") or item["due"])
                ref = datetime.fromisoformat(item.get("last_fired") or item.get("created") or item["due"])
                try:
                    nxt = trigger_for(item["repeat"], created).get_next_fire_time(None, _aware(ref))
                except Exception:  # noqa: BLE001
                    log.exception("bad recurring reminder %s", item.get("id"))
                    continue
                if nxt is not None and _naive(nxt) <= now:
                    threading.Thread(target=_fire, args=(item["id"], True), daemon=True).start()     # missed while Vex was off
                _schedule(item)
            elif datetime.fromisoformat(item["due"]) <= now:
                threading.Thread(target=_fire, args=(item["id"], True), daemon=True).start()
            else:
                _schedule(item)
        _scheduler.add_job(_sweep, "interval", seconds=SWEEP_SECONDS, id="_sweep", replace_existing=True)


def stop() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


# ---- tools ----------------------------------------------------------------------------------------------------------------------------------
@tool("set_reminder", "Set a reminder, e.g. 'in 20 minutes', 'tomorrow 5pm', 'every weekday at 9'.",
      {"text": {"type": "string", "description": "What to remind the user about"},
       "when": {"type": "string", "description": "When, in natural language; may repeat ('every Monday at 7pm')"}},
      describe=lambda a: f"Set reminder '{a['text']}' for {a['when']}")
def set_reminder(text: str, when: str) -> ToolResult:
    text = " ".join(text.split())
    if not text:
        return ToolResult(False, "The reminder is empty.")
    if find_secrets(text):
        return ToolResult(False, "Not saving that: it looks like it contains a secret.")
    now = datetime.now().replace(microsecond=0)
    rule = None
    if is_recurring(when):
        try:
            rule = parse_recurrence(when)
        except ValueError as e:
            return ToolResult(False, str(e))
        due = next_occurrence(rule, now, now)
        if due is None:
            return ToolResult(False, "I couldn't work out when that repeats next.")
    else:
        due = dateparser.parse(when, settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False})
        if due is None:
            return ToolResult(False, f"I couldn't understand the time '{when}'.")
        if due <= now:
            return ToolResult(False, f"{due:%A %d %B, %H:%M} has already passed.")
    start()
    with _lock:
        items = _load()
        if sum(1 for i in items if not i.get("done")) >= MAX_REMINDERS:
            return ToolResult(False, f"You already have {MAX_REMINDERS} reminders. Cancel some first.")
        item = {"id": uuid.uuid4().hex[:8], "text": text, "due": due.isoformat(timespec="seconds"), "done": False,
                "created": now.isoformat(timespec="seconds")}
        if rule:
            item["repeat"] = rule
        _save([*items, item])
        _schedule(item)
    if rule:
        return ToolResult(True, f"I'll remind you {describe_rule(rule)}: {text}. The next one is {due:%A %d %B at %H:%M}.",
                          {"id": item["id"], "due": item["due"], "repeat": describe_rule(rule)})
    return ToolResult(True, f"I'll remind you on {due:%A %d %B at %H:%M}: {text}", {"id": item["id"], "due": due.isoformat()})


def pending() -> list[dict]:
    """Open reminders in the order they come due (the numbering list_reminders and cancel_reminder share)."""
    return sorted((i for i in _load() if not i.get("done")), key=lambda i: i["due"])


def _line(n: int, item: dict) -> str:
    due = datetime.fromisoformat(item["due"])
    when = f"{describe_rule(item['repeat'])}, next {due:%a %d %b %H:%M}" if item.get("repeat") else f"{due:%a %d %b %H:%M}"
    return f"{n}. {item['text']} ({when})"


@tool("list_reminders", "List the reminders that are set.", {})
def list_reminders() -> ToolResult:
    items = pending()
    if not items:
        return ToolResult(True, "You have no reminders.", [])
    return ToolResult(True, "\n".join(_line(n, i) for n, i in enumerate(items, 1)),
                      [{"number": n, "text": i["text"], "due": i["due"], "repeat": bool(i.get("repeat"))} for n, i in enumerate(items, 1)])


def _cancel_classify(a: dict):
    return Decision(YELLOW, "this cancels every reminder") if a.get("all") else Decision("green")


@tool("cancel_reminder", "Cancel a reminder by number or words from its text (or all of them).",
      {"which": {"type": "string", "description": "Reminder number or part of its text", "default": ""},
       "all": {"type": "boolean", "description": "Cancel every reminder (the user is asked)", "default": False}},
      classify=_cancel_classify,
      describe=lambda a: "Cancel ALL reminders" if a.get("all") else f"Cancel reminder: {a.get('which', '')}")
def cancel_reminder(which: str = "", all: bool = False) -> ToolResult:  # noqa: A002 - the argument name is part of the tool schema
    items = pending()
    if not items:
        return ToolResult(False, "You have no reminders to cancel.")
    if all:
        picks = items
    else:
        which = (which or "").strip()
        if not which:
            return ToolResult(False, "Which reminder? " + " ".join(_line(n, i) for n, i in enumerate(items, 1)))
        if which.isdigit():
            if not 1 <= int(which) <= len(items):
                return ToolResult(False, f"There is no reminder number {which}. You have {len(items)}.")
            picks = [items[int(which) - 1]]
        else:
            scored = sorted(((fuzz.WRatio(which, i["text"]), i) for i in items), key=lambda x: -x[0])
            good = [i for s, i in scored if s >= 70]
            if not good:
                return ToolResult(False, f"I couldn't find a reminder matching '{which}'.")
            if len(good) > 1 and scored[0][0] - scored[1][0] < 8:
                return ToolResult(False, "That matches more than one: " + " ".join(_line(items.index(i) + 1, i) for i in good[:4])
                                  + " Which number?")
            picks = [good[0]]
    ids = {p["id"] for p in picks}
    with _lock:
        now = datetime.now().isoformat(timespec="seconds")
        out = []
        for i in _load():
            if i["id"] in ids and not i.get("done"):
                i["done"], i["cancelled"], i["done_at"] = True, True, now
            out.append(i)
        _save(out)
        for rid in ids:
            _unschedule(rid)
    return ToolResult(True, f"Cancelled {len(picks)} reminder{'s' if len(picks) != 1 else ''}: " + "; ".join(p["text"] for p in picks) + ".")
