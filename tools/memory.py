"""Agent tools over VexMind. Audit entries for these tools never contain fact text or queries: titles and counts only.

`forget` is yellow: prepare() builds the preview (read-only), the user approves exactly that preview, and only then does
the tool delete. The tool takes no approval-style arguments; `mentions` only chooses what happens to facts that merely
mention the person (remove just the name, or delete the fact).
"""
from __future__ import annotations

import logging

from memory import Memory, Result
from tools.registry import YELLOW, Refuse, ShortCircuit, ToolResult, tool

log = logging.getLogger("vex.tools.memory")
_memory: Memory | None = None


def configure(mem: Memory | None) -> None:
    """Use this Memory instance (the agent passes its own; tests pass a temp vault)."""
    global _memory
    _memory = mem


def get_memory() -> Memory:
    global _memory
    if _memory is None:
        _memory = Memory(watch=True)
    return _memory


def _wrap(res: Result, audit: str | None = None) -> ToolResult:
    return ToolResult(res.ok, res.message, res.data if res.ok else None, audit=audit)


@tool("remember",
      "Save a durable fact (person, project, preference, plan). Never secrets.",
      {"fact": {"type": "string", "description": "One short fact, e.g. 'Roommate since 2024'"},
       "about": {"type": "string", "description": "Note name, e.g. 'Rahul'; 'me' for the user", "default": ""},
       "note_type": {"type": "string", "enum": ["person", "project", "topic", "place", "preference", "profile"],
                     "description": "Type, if a new note is created", "default": ""},
       "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags", "default": []},
       "aliases": {"type": "array", "items": {"type": "string"}, "description": "Other names, e.g. 'my roommate'", "default": []}},
      audit_args=False)
def remember(fact: str, about: str = "", note_type: str = "", tags: list[str] | None = None,
             aliases: list[str] | None = None) -> ToolResult:
    res = get_memory().remember(fact, about=about or None, note_type=note_type or None, tags=tags or [],
                                aliases=aliases or [], source="user")
    audit = f"note={res.data['note']} action={res.data['action']}" if res.ok and res.data else None
    return _wrap(res, audit)


@tool("recall", "Look up what is known about a topic or person in memory.",
      {"query": {"type": "string", "description": "What to look up"}}, audit_args=False)
def recall(query: str) -> ToolResult:
    res = get_memory().recall(query)
    return _wrap(res, f"facts={len(res.data or [])}")


@tool("read_note", "Read a whole memory note by its title or alias.", {"title": {"type": "string", "description": "Note title"}},
      audit_args=False)
def read_note(title: str) -> ToolResult:
    return _wrap(get_memory().read_note(title), "note read")


@tool("link_notes", "Link two memory notes together, with an optional reason.",
      {"a": {"type": "string", "description": "First note"}, "b": {"type": "string", "description": "Second note"},
       "reason": {"type": "string", "description": "Why they are related", "default": ""}}, audit_args=False)
def link_notes(a: str, b: str, reason: str = "") -> ToolResult:
    return _wrap(get_memory().link_notes(a, b, reason or None), "notes linked")


@tool("list_notes", "List memory notes, optionally by type or tag.",
      {"type": {"type": "string", "description": "person, project, topic, place...", "default": ""},
       "tag": {"type": "string", "description": "Tag", "default": ""}}, audit_args=False)
def list_notes(type: str = "", tag: str = "") -> ToolResult:  # noqa: A002 - matches the tool schema
    res = get_memory().list_notes(type or None, tag or None)
    return _wrap(res, f"notes={len(res.data or [])}")


@tool("memory_stats", "Counts of notes, facts and links in memory.", {}, audit_args=False)
def memory_stats() -> ToolResult:
    return _wrap(get_memory().memory_stats(), "stats read")


def _prepare_forget(a: dict) -> dict:
    res = get_memory().forget(a["query"])
    if not res.needs_confirmation:
        raise ShortCircuit(ToolResult(True, res.message, audit="nothing matched"))
    mentions = a.get("mentions") or "redact"
    if mentions not in ("redact", "delete"):
        raise Refuse("mentions must be 'redact' or 'delete'.")
    return {"query": a["query"], "mentions": mentions}


def _describe_forget(a: dict) -> str:
    preview = get_memory().forget(a["query"]).message
    how = ("remove just their name" if a["mentions"] == "redact" else "delete the whole fact")
    return f"Forget from memory ({how} where they are only mentioned):\n{preview}"


@tool("forget",
      "Forget facts, or everything about a person. The user approves a preview.",
      {"query": {"type": "string", "description": "What to forget, e.g. 'Rahul'"},
       "mentions": {"type": "string", "enum": ["redact", "delete"],
                    "description": "redact (default) removes just the name; delete drops the fact",
                    "default": "redact"}},
      tier=YELLOW, prepare=_prepare_forget, describe=_describe_forget, audit_args=False)
def forget(query: str, mentions: str = "redact") -> ToolResult:
    res = get_memory().forget(query, confirm=True, mentions=mentions)       # approval was already given to the preview
    d = res.data or {}
    audit = f"notes={d.get('notes', [])} deleted={d.get('deleted', 0)} redacted={d.get('redacted', 0)}"
    return ToolResult(res.ok, res.message, audit=audit)
