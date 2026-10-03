"""Tasks: a markdown checklist in the vault (VexVault/tasks.md), editable by hand."""
from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz

from memory.secrets import find_secrets
from memory.vault import Vault
from tools.registry import ToolResult, tool

TASK_RE = re.compile(r"^- \[(?P<done>[ xX])\] (?P<text>.+?)\s*$")


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
    line = f"- [ ] {text}" + (f" (due {due.strip()})" if due.strip() else "")
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
