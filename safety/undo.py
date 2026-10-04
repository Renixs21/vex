"""Undo for file actions: create, edit, copy, move and delete. Memory edits are not undoable (except notes in the Recycle Bin).

Undo never overwrites anything: it refuses if the original location is occupied. Removals go to the Recycle Bin, so
even an undo can be reversed. Deleted files are restored by reading the Recycle Bin's own $I/$R records.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from memory.vault import atomic_write

log = logging.getLogger("vex.undo")
LIMIT = 20
_FILETIME_EPOCH = 116444736000000000


@dataclass
class BinRecord:
    path: str
    deleted: int            # FILETIME
    info_file: Path


def parse_info_file(p: Path) -> BinRecord:
    """Parse a Recycle Bin $I file (format v1 for Vista-8, v2 for Windows 10+)."""
    raw = p.read_bytes()
    version = struct.unpack_from("<q", raw, 0)[0]
    deleted = struct.unpack_from("<q", raw, 16)[0]
    if version == 2:
        n = struct.unpack_from("<i", raw, 24)[0]
        text = raw[28:28 + n * 2].decode("utf-16-le")
    else:
        text = raw[24:24 + 520].decode("utf-16-le")
    return BinRecord(text.split("\x00", 1)[0], deleted, p)


def restore_from_recycle_bin(original: str | Path) -> tuple[bool, str]:
    """Move the most recently deleted item with this original path back to where it was."""
    original = Path(original)
    drive = os.path.splitdrive(str(original))[0]
    if not drive:
        return False, "I can't tell which drive's Recycle Bin to look in."
    bin_root = Path(drive + "\\") / "$Recycle.Bin"
    matches: list[BinRecord] = []
    for attempt in range(4):                       # the bin's record can appear a moment after the delete returns
        try:
            sid_dirs = [d for d in bin_root.iterdir() if d.is_dir()]
        except OSError as e:
            return False, f"Couldn't open the Recycle Bin: {e}"
        for sid in sid_dirs:
            try:
                for info in sid.glob("$I*"):
                    try:
                        rec = parse_info_file(info)
                    except (OSError, struct.error, UnicodeDecodeError):
                        continue
                    if os.path.normcase(rec.path) == os.path.normcase(str(original)):
                        matches.append(rec)
            except OSError:
                continue                           # another user's bin: not accessible, and not ours to restore
        if matches:
            break
        time.sleep(0.3)
    if not matches:
        return False, f"{original.name} isn't in the Recycle Bin any more."
    rec = max(matches, key=lambda r: r.deleted)
    trashed = rec.info_file.with_name("$R" + rec.info_file.name[2:])
    if not trashed.exists():
        return False, "The Recycle Bin entry is damaged."
    if original.exists():
        return False, f"Something already exists at {original}; not overwriting it."
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(trashed), str(original))
    rec.info_file.unlink(missing_ok=True)
    return True, f"Restored {original}."


class UndoStack:
    """The last few undoable actions, persisted so 'undo that' works after a restart."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, items: list[dict]) -> None:
        atomic_write(self.path, json.dumps(items[-LIMIT:], ensure_ascii=False, indent=1))

    def push(self, audit_id: str, undo: dict) -> None:
        """`undo` is {'kind': created|copied|moved|deleted, ...paths, 'label': text}."""
        with self._lock:
            items = self._load()
            items.append({"audit_id": audit_id, **undo})
            self._save(items)

    def peek(self) -> dict | None:
        items = self._load()
        return items[-1] if items else None

    def undo_last(self) -> tuple[bool, str, str | None]:
        """Reverse the most recent undoable action. Returns (ok, message, audit_id of the action reversed)."""
        with self._lock:
            items = self._load()
            if not items:
                return False, "There is nothing to undo.", None
            entry = items[-1]
            ok, msg = self._apply(entry)
            if ok:
                self._save(items[:-1])
            return ok, msg, entry.get("audit_id")

    @staticmethod
    def _undo_edit(e: dict) -> tuple[bool, str]:
        """Put back the text the file had before the edit. Refuses if the file was changed again since (nothing is ever lost)."""
        import hashlib

        p, backup = Path(e["path"]), Path(e["backup"])
        if not backup.exists():
            return False, "The saved copy from before the edit is gone, so I can't undo it."
        if not p.exists():
            return False, f"{p.name} isn't there any more."
        if hashlib.sha256(p.read_bytes()).hexdigest() != e.get("after_sha"):
            return False, f"{p.name} has been changed since my edit, so I won't overwrite it."
        data = backup.read_bytes()
        tmp = p.with_name(f".{p.name}.undo.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(p)
        finally:
            tmp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        return True, f"Restored the earlier version of {p.name}."

    @staticmethod
    def _apply(e: dict) -> tuple[bool, str]:
        kind = e["kind"]
        try:
            if kind in ("created", "copied"):
                p = Path(e["path"])
                if not p.exists():
                    return True, f"{p.name} was already gone."
                send2trash(str(p))
                return True, f"Moved {p.name} to the Recycle Bin."
            if kind == "moved":
                src, dst = Path(e["src"]), Path(e["dst"])
                if not dst.exists():
                    return False, f"{dst.name} isn't where I left it, so I can't move it back."
                if src.exists():
                    return False, f"Something already exists at {src}; not overwriting it."
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                return True, f"Moved {src.name} back to {src.parent}."
            if kind == "deleted":
                return restore_from_recycle_bin(e["path"])
            if kind == "edited":
                return UndoStack._undo_edit(e)
            if kind == "task_deleted":
                from tools import tasks  # local import: tools import this module

                tasks.restore_line(e["line"], int(e["index"]))
                return True, "Put the task back on your list."
        except OSError as ex:
            return False, f"Undo failed: {ex}"
        return False, f"I don't know how to undo '{kind}'."


_stack: UndoStack | None = None


def configure(stack: UndoStack) -> None:
    global _stack
    _stack = stack


def get_stack() -> UndoStack:
    global _stack
    if _stack is None:
        from config import ROOT, settings

        _stack = UndoStack(ROOT / settings["paths"]["logs"] / "undo.json")
    return _stack
