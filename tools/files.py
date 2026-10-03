"""File tools. Tiers come from safety.policy (safe folders green, elsewhere yellow, protected red); delete is always red."""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from send2trash import send2trash

from safety import policy
from safety.paths import known_folder, resolve_path  # noqa: F401  (re-exported for callers and tests)
from safety.undo import get_stack
from tools.registry import GREEN, RED, Refuse, ToolResult, tool

MAX_WRITE_CHARS = 1_000_000
MAX_READ_BYTES = 2_000_000
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
@tool("create_file",
      "Create a text file (e.g. Desktop/notes.txt). Won't overwrite.",
      {"path": {"type": "string", "description": "e.g. Desktop/notes.txt"},
       "content": {"type": "string", "description": "Text to write"}},
      prepare=lambda a: _abs(a, "path"),
      classify=lambda a: policy.path_decision(a["path"], "write"),
      describe=lambda a: f"Create file: {a['path']}  ({len(a['content']):,} characters)")
def create_file(path: str, content: str) -> ToolResult:
    p = resolve_path(path)
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult(False, "That's too much text to write in one go.")
    if p.exists():
        return ToolResult(False, f"{p} already exists; not overwriting.")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        # LF is kept as given; batch files get CRLF because cmd.exe labels and goto can misbehave with LF-only lines
        crlf = p.suffix.lower() in CRLF_SUFFIXES
        newline = "\r\n" if crlf else ""
        text = content.replace("\r\n", "\n") if crlf else content        # avoid doubling existing CRLFs
        with open(tmp, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)
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


@tool("undo_last", "Undo the last file action (create, copy, move or delete).", {}, tier=GREEN,
      describe=lambda a: "Undo the last file action")
def undo_last() -> ToolResult:
    ok, msg, reversed_id = get_stack().undo_last()
    return ToolResult(ok, msg, audit=f"undid {reversed_id}" if ok and reversed_id else None)
