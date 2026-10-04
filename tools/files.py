"""File tools. Tiers come from safety.policy (safe folders green, elsewhere yellow, protected red); delete is always red."""
from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

from send2trash import send2trash

from safety import policy
from safety.paths import known_folder, resolve_path  # noqa: F401  (re-exported for callers and tests)
from safety.undo import get_stack
from safety.policy import Decision
from tools.registry import GREEN, RED, YELLOW, Refuse, ToolResult, tool

MAX_WRITE_CHARS = 1_000_000
MAX_READ_BYTES = 2_000_000
MAX_EDIT_BYTES = 1_000_000
CRLF_SUFFIXES = {".bat", ".cmd"}


def _abs(args: dict, *keys: str) -> dict:
    """prepare() helper: turn path arguments into absolute paths, so the user sees exactly what will be touched."""
    out = dict(args)
    for k in keys:
        if out.get(k):
            out[k] = str(resolve_path(str(out[k])))
    return out


def _size_text(p: Path) -> str:
    try:
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*"))
            return f"folder with {n} item(s)"
        b = p.stat().st_size
        return f"{b:,} bytes"
    except OSError:
        return "unknown size"


# ---- create ---------------------------------------------------------------------------------------------------------
def _prepare_create(a: dict) -> dict:
    a = _abs(a, "path")
    p = Path(a["path"])
    if p.is_dir():
        raise Refuse(f"{p} is a folder, not a file.")
    if p.exists():
        a["_replace"] = True                  # not an LLM argument (it is added after the arguments were filtered): decides the question
    return a


def _next_free_name(p: Path) -> Path:
    """'names.txt' -> 'names (2).txt' (or the next number that is free)."""
    n = 2
    while True:
        cand = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _create_classify(a: dict):
    d = policy.path_decision(a["path"], "write")
    if a.get("_replace"):                     # replacing an existing file always asks, in any folder; protected places never
        if d.tier == RED:
            return Decision(RED, d.reason, blocked=True)
        return Decision(YELLOW, "the file already exists")
    return d


def _create_describe(a: dict) -> str:
    if not a.get("_replace"):
        return f"Create file: {a['path']}  ({len(a['content']):,} characters)"
    p = Path(a["path"])
    try:
        old = _read_text(p)[0]
    except Refuse:
        old = ""
    return (f"{p.name} already exists. Replace it (No saves a copy as {_next_free_name(p).name})\n"
            f"Yes: replace {p} with the new text; the old version is kept so 'undo that' brings it back.\n"
            f"No: save the new text as {_next_free_name(p)} instead.\n" + _preview(old, a["content"]))


def _decline_create(a: dict) -> dict | None:
    if not a.get("_replace"):
        return None
    return {"path": str(_next_free_name(Path(a["path"]))), "content": a["content"]}


@tool("create_file",
      "Create a text file (e.g. Desktop/notes.txt). Asks before replacing an existing one.",
      {"path": {"type": "string", "description": "e.g. Desktop/notes.txt"},
       "content": {"type": "string", "description": "Text to write"}},
      prepare=_prepare_create, classify=_create_classify, describe=_create_describe, on_decline=_decline_create)
def create_file(path: str, content: str, _replace: bool = False) -> ToolResult:
    p = resolve_path(path)
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult(False, "That's too much text to write in one go.")
    if p.exists() and not _replace:
        return ToolResult(False, f"{p} already exists; not overwriting.")
    if p.exists():                            # approved replacement: keep the old version for undo
        return _write_with_backup(p, content, f"replace {p.name}", verb="Replaced")
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, content)
    return ToolResult(True, f"Created {p}", {"path": str(p)}, undo={"kind": "created", "path": str(p), "label": f"create {p.name}"})


# ---- read / list ----------------------------------------------------------------------------------------------------
@tool("read_file", "Read a text file. Won't read secret or credential files.",
      {"path": {"type": "string", "description": "File path"},
       "max_chars": {"type": "integer", "description": "Max characters", "default": 20000}},
      prepare=lambda a: _abs(a, "path"),
      classify=lambda a: policy.path_decision(a["path"], "read"),
      describe=lambda a: f"Read file: {a['path']}")
def read_file(path: str, max_chars: int = 20000) -> ToolResult:
    p = resolve_path(path)
    if not p.is_file():
        return ToolResult(False, f"{p} is not a file.")
    if p.stat().st_size > MAX_READ_BYTES:
        return ToolResult(False, f"{p.name} is larger than {MAX_READ_BYTES // 1_000_000} MB; too big to read.")
    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        return ToolResult(False, f"{p.name} looks like a binary file, not text.")
    text = raw.decode("utf-8", errors="replace")
    max_chars = max(1, min(int(max_chars), 50000))
    cut = len(text) > max_chars
    return ToolResult(True, text[:max_chars] + ("\n...[truncated]" if cut else ""), {"path": str(p), "truncated": cut})


@tool("list_dir", "List the files and folders in a directory.",
      {"path": {"type": "string", "description": "Folder path, e.g. Downloads", "default": "Desktop"}},
      prepare=lambda a: _abs({"path": a.get("path") or "Desktop"}, "path"),
      classify=lambda a: policy.path_decision(a["path"], "list"),
      describe=lambda a: f"List folder: {a['path']}")
def list_dir(path: str = "Desktop") -> ToolResult:
    p = resolve_path(path)
    if not p.is_dir():
        return ToolResult(False, f"{p} is not a folder.")
    entries = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
        entries.append({"name": e.name, "type": "folder" if e.is_dir() else "file",
                        "bytes": None if e.is_dir() else e.stat().st_size})
    lines = [f"{'[dir] ' if e['type'] == 'folder' else ''}{e['name']}" for e in entries]
    return ToolResult(True, "\n".join(lines) or "(empty)", entries)


# ---- move / copy ----------------------------------------------------------------------------------------------------
def _dest(args: dict) -> dict:
    a = _abs(args, "src", "dst")
    if Path(a["dst"]).is_dir():
        a["dst"] = str(Path(a["dst"]) / Path(a["src"]).name)
    return a


@tool("move_file", "Move or rename a file or folder. Refuses to overwrite.",
      {"src": {"type": "string", "description": "Existing path"}, "dst": {"type": "string", "description": "New path or destination folder"}},
      prepare=_dest,
      classify=lambda a: policy.paths_decision([(a["src"], "delete"), (a["dst"], "write")]),
      describe=lambda a: f"Move: {a['src']}\n  to: {a['dst']}")
def move_file(src: str, dst: str) -> ToolResult:
    s, d = Path(src), Path(dst)
    if not s.exists():
        return ToolResult(False, f"{s} doesn't exist.")
    if d.exists():
        return ToolResult(False, f"{d} already exists; not overwriting.")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return ToolResult(True, f"Moved {s.name} to {d}", {"path": str(d)},
                      undo={"kind": "moved", "src": str(s), "dst": str(d), "label": f"move {s.name}"})


@tool("copy_file", "Copy a file or folder. Refuses to overwrite.",
      {"src": {"type": "string", "description": "Existing path"}, "dst": {"type": "string", "description": "New path or destination folder"}},
      prepare=_dest,
      classify=lambda a: policy.paths_decision([(a["src"], "read"), (a["dst"], "write")]),
      describe=lambda a: f"Copy: {a['src']}\n  to: {a['dst']}")
def copy_file(src: str, dst: str) -> ToolResult:
    s, d = Path(src), Path(dst)
    if not s.exists():
        return ToolResult(False, f"{s} doesn't exist.")
    if d.exists():
        return ToolResult(False, f"{d} already exists; not overwriting.")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d)
    return ToolResult(True, f"Copied {s.name} to {d}", {"path": str(d)},
                      undo={"kind": "copied", "path": str(d), "label": f"copy {s.name}"})


# ---- edit (append / replace / overwrite) ------------------------------------------------------------------------------
def _atomic_write_text(p: Path, text: str, newline: str | None = None) -> None:
    """Write via a temp file in the same folder, then replace. LF stays LF; batch files get CRLF (cmd.exe needs it)."""
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:6]}.tmp")
    crlf = newline == "\r\n" or (newline is None and p.suffix.lower() in CRLF_SUFFIXES)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\r\n" if crlf else "") as f:
            f.write(text.replace("\r\n", "\n") if crlf else text)
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)


def _read_text(p: Path) -> tuple[str, str]:
    """(text with LF newlines, the file's newline style). Refuses binary or huge files."""
    if not p.is_file():
        raise Refuse(f"{p} is not a file.")
    if p.stat().st_size > MAX_EDIT_BYTES:
        raise Refuse(f"{p.name} is larger than {MAX_EDIT_BYTES // 1_000_000} MB; too big to edit.")
    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        raise Refuse(f"{p.name} looks like a binary file, not text.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse(f"{p.name} isn't UTF-8 text, so I won't edit it.") from None
    style = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), style


def _preview(old: str, new: str, max_lines: int = 14) -> str:
    """A short unified diff (what will change) for the confirmation dialog."""
    diff = list(difflib.unified_diff(old.split("\n"), new.split("\n"), "before", "after", n=1, lineterm=""))
    body = [l for l in diff if not l.startswith(("---", "+++"))]
    if not body:
        return "(no visible change)"
    shown = [l[:160] for l in body[:max_lines]]
    if len(body) > max_lines:
        shown.append(f"... {len(body) - max_lines} more changed line(s)")
    return "\n".join(shown)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _backup_dir() -> Path:
    from config import ROOT

    d = ROOT / "data" / "undo_backups"
    d.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    for old in d.glob("*.bak"):                         # backups only matter for 'undo that', which keeps the last 20 actions
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return d


def _write_with_backup(p: Path, new_text: str, label: str, verb: str = "Edited") -> ToolResult:
    """Replace a text file's content, keeping the previous version so undo_last can restore it exactly."""
    _, style = _read_text(p)
    backup = _backup_dir() / f"{uuid.uuid4().hex}.bak"
    shutil.copy2(p, backup)
    _atomic_write_text(p, new_text, style)
    return ToolResult(True, f"{verb} {p}", {"path": str(p)},
                      undo={"kind": "edited", "path": str(p), "backup": str(backup), "after_sha": _sha(p), "label": label})


def _prepare_edit(a: dict) -> dict:
    a = _abs(a, "path")
    p = Path(a["path"])
    if not p.exists():
        raise Refuse(f"{p} doesn't exist. Use create_file to make a new file.")
    _read_text(p)                                        # refuses folders, binaries, huge or non-UTF-8 files up front
    return a


def _edit_classify(a: dict):
    """Edits always ask (yellow), in any folder. Protected places and secret files are refused outright."""
    d = policy.path_decision(a["path"], "write")
    if d.tier == RED:
        return Decision(RED, d.reason, blocked=True)
    return Decision(YELLOW, d.reason or "edits an existing file")


def _joined(old: str, add: str) -> str:
    if not old or old.endswith("\n"):
        return old + add
    return old + "\n" + add


@tool("append_to_file", "Add text to the end of an existing text file. The user sees a preview and approves.",
      {"path": {"type": "string", "description": "Existing file, e.g. Desktop/notes.txt"},
       "text": {"type": "string", "description": "Text to add (a new line is started if needed)"}}, tier=YELLOW,
      prepare=_prepare_edit, classify=_edit_classify,
      describe=lambda a: (f"Add to the end of {Path(a['path']).name} ({len(a['text']):,} characters)\n{a['path']}\n"
                          + _preview(_read_text(Path(a["path"]))[0], _joined(_read_text(Path(a["path"]))[0], a["text"].replace("\r\n", "\n")))))
def append_to_file(path: str, text: str) -> ToolResult:
    p = resolve_path(path)
    old, _ = _read_text(p)
    new = _joined(old, text.replace("\r\n", "\n"))
    if len(new) > MAX_WRITE_CHARS:
        return ToolResult(False, "That would make the file too large.")
    return _write_with_backup(p, new, f"append to {p.name}", verb="Added text to")


def _prepare_replace(a: dict) -> dict:
    a = _prepare_edit(a)
    old, _ = _read_text(Path(a["path"]))
    target = a["old_text"].replace("\r\n", "\n")
    if not target:
        raise Refuse("old_text is empty.")
    n = old.count(target)
    if n == 0:
        raise Refuse(f"I couldn't find that text in {Path(a['path']).name}. Read the file and quote the exact text.")
    if n > 1 and not a.get("replace_all"):
        raise Refuse(f"That text appears {n} times in {Path(a['path']).name}. Give a longer, unique piece, or ask to replace all of them.")
    return a


def _replace_result(a: dict) -> tuple[str, str]:
    old, _ = _read_text(Path(a["path"]))
    target, new_text = a["old_text"].replace("\r\n", "\n"), a["new_text"].replace("\r\n", "\n")
    return old, old.replace(target, new_text) if a.get("replace_all") else old.replace(target, new_text, 1)


@tool("replace_in_file", "Replace text inside an existing text file. The user sees a preview and approves.",
      {"path": {"type": "string", "description": "Existing file"},
       "old_text": {"type": "string", "description": "Exact text to find"},
       "new_text": {"type": "string", "description": "Text to put there"},
       "replace_all": {"type": "boolean", "description": "Replace every occurrence (default: only one, which must be unique)", "default": False}},
      tier=YELLOW, prepare=_prepare_replace, classify=_edit_classify,
      describe=lambda a: f"Change text in {Path(a['path']).name}\n{a['path']}\n" + _preview(*_replace_result(a)))
def replace_in_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    p = resolve_path(path)
    old, new = _replace_result({"path": str(p), "old_text": old_text, "new_text": new_text, "replace_all": replace_all})
    if len(new) > MAX_WRITE_CHARS:
        return ToolResult(False, "That would make the file too large.")
    return _write_with_backup(p, new, f"edit {p.name}", verb="Changed text in")


@tool("overwrite_file", "Replace the whole content of an existing text file. The user sees a preview and approves.",
      {"path": {"type": "string", "description": "Existing file"}, "content": {"type": "string", "description": "The new full content"}},
      tier=YELLOW, prepare=_prepare_edit, classify=_edit_classify,
      describe=lambda a: (f"Replace the whole content of {Path(a['path']).name} ({len(a['content']):,} characters)\n{a['path']}\n"
                          + _preview(_read_text(Path(a["path"]))[0], a["content"].replace("\r\n", "\n"))))
def overwrite_file(path: str, content: str) -> ToolResult:
    p = resolve_path(path)
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult(False, "That's too much text to write in one go.")
    return _write_with_backup(p, content.replace("\r\n", "\n"), f"overwrite {p.name}", verb="Replaced the content of")


# ---- delete / undo --------------------------------------------------------------------------------------------------
def _prepare_delete(a: dict) -> dict:
    a = _abs(a, "path")
    if not os.path.lexists(a["path"]):
        raise Refuse(f"{a['path']} doesn't exist.")
    return a


@tool("delete_file", "Delete a file or folder to the Recycle Bin.",
      {"path": {"type": "string", "description": "Path to delete"}}, tier=RED,
      prepare=_prepare_delete,
      classify=lambda a: policy.path_decision(a["path"], "delete"),
      describe=lambda a: f"Delete (to the Recycle Bin): {a['path']}  [{_size_text(Path(a['path']))}]")
def delete_file(path: str) -> ToolResult:
    p = Path(path)
    if not os.path.lexists(p):
        return ToolResult(False, f"{p} doesn't exist.")
    send2trash(str(p))
    return ToolResult(True, f"Moved {p.name} to the Recycle Bin.", {"path": str(p)},
                      undo={"kind": "deleted", "path": str(p), "label": f"delete {p.name}"})


@tool("undo_last", "Undo the last file action (create, edit, copy, move or delete).", {}, tier=GREEN,
      describe=lambda a: "Undo the last file action")
def undo_last() -> ToolResult:
    ok, msg, reversed_id = get_stack().undo_last()
    return ToolResult(ok, msg, audit=f"undid {reversed_id}" if ok and reversed_id else None)
