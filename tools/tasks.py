"""Tasks: a markdown checklist in the vault (VexVault/tasks.md), editable by hand. Add, list, complete, edit, delete, and a "what's on today"."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import dateparser
from rapidfuzz import fuzz

from memory.secrets import find_secrets
from memory.vault import Vault
from tools.registry import Refuse, ToolResult, tool

TASK_RE = re.compile(r"^- \[(?P<done>[ xX])\] (?P<text>.+?)\s*$")
DUE_RE = re.compile(r"\s*\(due (?P<due>[^)]*)\)\s*$")


def split_due(text: str) -> tuple[str, str]:
    """'buy milk (due 2026-10-05)' -> ('buy milk', '2026-10-05')."""
    m = DUE_RE.search(text)
    return (text[:m.start()].rstrip(), m.group("due").strip()) if m else (text, "")


def normalize_due(due: str) -> str:
    """'tomorrow' -> '2026-10-05' (with the time when one was given); text that isn't a date is kept as written."""
    due = (due or "").strip()
    if not due:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", due):
        return due
    parsed = dateparser.parse(due, settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False})
    if parsed is None:
        return due
    has_time = bool(re.search(r"\d\s*(?::\d\d)?\s*(?:am|pm)\b|\d:\d\d|\bnoon\b|\bmidnight\b|\bat \d", due, re.I))
    return f"{parsed:%Y-%m-%d %H:%M}" if has_time else f"{parsed:%Y-%m-%d}"


def due_date(due: str) -> date | None:
    try:
        return datetime.fromisoformat(due.strip()).date()
    except ValueError:
        return None


def vault_root() -> Path:
    from config import ROOT, settings

    return ROOT / settings["paths"]["vault"]


def _open() -> tuple[Vault, Path]:
    vault = Vault(vault_root())
    path = vault.root / "tasks.md"
    if not path.exists():
        vault.create("task-list", "Tasks")
    else:
        vault.ensure_identity(path)
    return vault, path


def _tasks(body: str) -> list[tuple[int, bool, str]]:
    """(line index, done, text) for each checklist line."""
    out = []
    for i, line in enumerate(body.split("\n")):
        if m := TASK_RE.match(line):
            out.append((i, m.group("done").lower() == "x", m.group("text")))
    return out


@tool("add_task", "Add a to-do item to the task list.",
      {"text": {"type": "string", "description": "The task"}, "due": {"type": "string", "description": "Optional due date or time as text", "default": ""}},
      describe=lambda a: f"Add task: {a['text']}")
def add_task(text: str, due: str = "") -> ToolResult:
    text = " ".join(text.split())
    if not text:
        return ToolResult(False, "The task is empty.")
    if find_secrets(text):
        return ToolResult(False, "Not saving that: it looks like it contains a secret.")
    due = normalize_due(due)
    line = f"- [ ] {text}" + (f" (due {due})" if due else "")
    vault, path = _open()

    def apply(note) -> None:
        body = note.body if note.body.endswith("\n") else note.body + "\n"
        note.body = body + line + "\n"

    vault.mutate(path, apply)
    return ToolResult(True, f"Added task: {text}")


@tool("list_tasks", "List open to-do items.",
      {"include_done": {"type": "boolean", "description": "Also show completed tasks", "default": False}})
def list_tasks(include_done: bool = False) -> ToolResult:
    vault, path = _open()
    items = [(n, done, text) for n, (_i, done, text) in enumerate(_tasks(vault.read(path).body), 1) if include_done or not done]
    if not items:
        return ToolResult(True, "No open tasks." if not include_done else "No tasks.", [])
    lines = [f"{n}. [{'x' if done else ' '}] {text}" for n, done, text in items]
    return ToolResult(True, "\n".join(lines), [{"number": n, "done": d, "text": t} for n, d, t in items])


@tool("complete_task", "Mark a task done by number or words from its text.",
      {"task": {"type": "string", "description": "Task number or part of its text"}},
      describe=lambda a: f"Complete task: {a['task']}")
def complete_task(task: str) -> ToolResult:
    vault, path = _open()
    items = _tasks(vault.read(path).body)
    open_items = [(n, i, text) for n, (i, done, text) in enumerate(items, 1) if not done]
    if not open_items:
        return ToolResult(False, "There are no open tasks.")
    pick = None
    if task.strip().isdigit():
        pick = next((t for t in open_items if t[0] == int(task.strip())), None)
    else:
        best = max(open_items, key=lambda t: fuzz.WRatio(task, t[2]))
        if fuzz.WRatio(task, best[2]) >= 70:
            pick = best
    if pick is None:
        return ToolResult(False, f"I couldn't find an open task matching '{task}'.")
    _n, line_idx, text = pick

    def apply(note) -> None:
        lines = note.body.split("\n")
        lines[line_idx] = lines[line_idx].replace("- [ ]", "- [x]", 1)
        note.body = "\n".join(lines)

    vault.mutate(path, apply)
    return ToolResult(True, f"Marked done: {text}")


# ---- edit / delete ----------------------------------------------------------------------------------------------------
def _resolve(task: str, include_done: bool = True) -> tuple[int, int, bool, str]:
    """(number, line index, done, text) of the task meant by 'a number or words from its text'. Raises Refuse with a helpful message."""
    vault, path = _open()
    items = [(n, i, done, text) for n, (i, done, text) in enumerate(_tasks(vault.read(path).body), 1) if include_done or not done]
    if not items:
        raise Refuse("There are no tasks.")
    task = (task or "").strip()
    if task.isdigit():
        hit = next((t for t in items if t[0] == int(task)), None)
        if hit is None:
            raise Refuse(f"There is no task number {task}.")
        return hit
    scored = sorted(((fuzz.WRatio(task, split_due(t[3])[0]), t) for t in items), key=lambda x: -x[0])
    if not scored or scored[0][0] < 70:
        raise Refuse(f"I couldn't find a task matching '{task}'.")
    if len(scored) > 1 and scored[1][0] >= 70 and scored[0][0] - scored[1][0] < 8:
        raise Refuse("That matches more than one task: " + "; ".join(f"{t[0]}. {split_due(t[3])[0]}" for _s, t in scored[:3]) + ". Which number?")
    return scored[0][1]


def _prepare_task(a: dict) -> dict:
    n, idx, done, text = _resolve(a["task"])
    return {**a, "_n": n, "_text": split_due(text)[0]}


@tool("edit_task", "Change a task's text and/or due date, by number or words from its text.",
      {"task": {"type": "string", "description": "Task number or part of its text"},
       "text": {"type": "string", "description": "New text (blank = keep)", "default": ""},
       "due": {"type": "string", "description": "New due date; 'none' removes it (blank = keep)", "default": ""}},
      prepare=_prepare_task,
      describe=lambda a: f"Edit task {a['_n']}: {a['_text']}")
def edit_task(task: str, text: str = "", due: str = "", _n: int = 0, _text: str = "") -> ToolResult:
    text = " ".join((text or "").split())
    if not text and not (due or "").strip():
        return ToolResult(False, "Tell me the new text or the new due date.")
    if text and find_secrets(text):
        return ToolResult(False, "Not saving that: it looks like it contains a secret.")
    vault, path = _open()
    n, idx, done, old = _resolve(str(_n) if _n else task)
    base, old_due = split_due(old)
    new_base = text or base
    new_due = old_due if not (due or "").strip() else ("" if due.strip().lower() in ("none", "clear", "remove", "no") else normalize_due(due))

    def apply(note) -> None:
        lines = note.body.split("\n")
        box = "x" if done else " "
        lines[idx] = f"- [{box}] {new_base}" + (f" (due {new_due})" if new_due else "")
        note.body = "\n".join(lines)

    vault.mutate(path, apply)
    return ToolResult(True, f"Updated task {n}: {new_base}" + (f", due {new_due}" if new_due else ""))


@tool("delete_task", "Delete a task from the list, by number or words from its text. The user is asked.",
      {"task": {"type": "string", "description": "Task number or part of its text"}}, tier="yellow",
      prepare=_prepare_task,
      describe=lambda a: f"Delete task {a['_n']}: {a['_text']}")
def delete_task(task: str, _n: int = 0, _text: str = "") -> ToolResult:
    vault, path = _open()
    n, idx, done, text = _resolve(str(_n) if _n else task)
    removed: dict = {}

    def apply(note) -> None:
        lines = note.body.split("\n")
        removed["line"] = lines[idx]
        del lines[idx]
        note.body = "\n".join(lines)

    vault.mutate(path, apply)
    return ToolResult(True, f"Deleted task: {split_due(text)[0]}", {"number": n},
                      undo={"kind": "task_deleted", "line": removed["line"], "index": idx, "label": f"delete task {split_due(text)[0][:30]}"})


def restore_line(line: str, index: int) -> bool:
    """Undo for delete_task: put the line back where it was (or at the end if the list changed)."""
    vault, path = _open()

    def apply(note) -> None:
        lines = note.body.split("\n")
        lines.insert(min(index, len(lines)), line)
        note.body = "\n".join(lines)

    vault.mutate(path, apply)
    return True


# ---- what's on today ----------------------------------------------------------------------------------------------------
@tool("today_summary", "Say what is on today: reminders, tasks due or overdue, and open tasks.", {})
def today_summary() -> ToolResult:
    from tools import reminders

    now = datetime.now()
    today = now.date()
    rems = [r for r in reminders.pending() if datetime.fromisoformat(r["due"]).date() == today]
    vault, path = _open()
    open_tasks = [split_due(t) for _i, done, t in _tasks(vault.read(path).body) if not done]
    due_today, overdue, later = [], [], []
    for text, due in open_tasks:
        d = due_date(due) if due else None
        if d is None:
            later.append(text)
        elif d < today:
            overdue.append(f"{text} (was due {d:%d %b})")
        elif d == today:
            due_today.append(text)
        else:
            later.append(text)
    parts = [f"Today is {now:%A %d %B}."]
    if rems:
        parts.append("Reminders: " + "; ".join(f"{datetime.fromisoformat(r['due']):%H:%M} {r['text']}" for r in rems) + ".")
    if overdue:
        parts.append("Overdue tasks: " + "; ".join(overdue) + ".")
    if due_today:
        parts.append("Tasks due today: " + "; ".join(due_today) + ".")
    if later:
        parts.append(f"{len(later)} other open task{'s' if len(later) != 1 else ''}" + (": " + "; ".join(later[:5]) if len(later) <= 5 else ", the first being " + later[0]) + ".")
    if not (rems or overdue or due_today or later):
        parts.append("Nothing is scheduled and you have no open tasks.")
    elif not (rems or overdue or due_today):
        parts.insert(1, "No reminders or deadlines today.")
    return ToolResult(True, " ".join(parts), {"reminders": len(rems), "due_today": len(due_today), "overdue": len(overdue), "open": len(open_tasks)})
