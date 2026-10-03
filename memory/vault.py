"""The vault: markdown notes on disk (the source of truth). Reads, writes, frontmatter, facts, atomic saves."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import frontmatter
from ulid import ULID

from memory import parser

log = logging.getLogger("vex.memory.vault")

HIDDEN_DIR = ".vexmind"
# type -> folder (None = fixed file at the vault root, see SINGLETONS)
TYPE_FOLDERS = {"person": "people", "project": "projects", "topic": "topics", "place": "places",
                "daily": "daily", "inbox": "inbox"}
SINGLETONS = {"profile": "profile.md", "preference": "preferences.md", "task-list": "tasks.md"}
FOLDER_TYPES = {v: k for k, v in TYPE_FOLDERS.items()}
FILE_TYPES = {v: k for k, v in SINGLETONS.items()}
NOTE_TYPES = [*TYPE_FOLDERS, *SINGLETONS]
_KNOWN_KEYS = {"id", "type", "title", "aliases", "tags", "created", "updated", "importance"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


@dataclass
class Note:
    id: str
    type: str
    title: str
    body: str
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    importance: int = 3
    extra: dict = field(default_factory=dict)     # unknown frontmatter keys, preserved on write
    path: Path | None = None

    @property
    def stem(self) -> str:
        return self.path.stem if self.path else parser.slugify(self.title)

    def parsed(self) -> parser.Parsed:
        return parser.parse_body(self.body)

    def names(self) -> set[str]:
        """Every normalized name this note answers to: filename stem, title and aliases."""
        return {parser.normalize(n) for n in [self.stem, self.title, *self.aliases] if n}


def _scalar(s: str) -> str:
    """YAML-safe scalar: plain when obviously safe, JSON-quoted otherwise."""
    if re.fullmatch(r"[\w][\w .'()&/-]*", s) and not re.fullmatch(r"[\d.]+|true|false|null|yes|no|on|off", s, re.I):
        return s
    return json.dumps(s, ensure_ascii=False)


def _flow_list(items: list[str]) -> str:
    return "[" + ", ".join(_scalar(str(i)) for i in items) + "]"


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, (datetime, date)) else str(v or "")


def dumps(note: Note) -> str:
    """Serialize a note to markdown with frontmatter."""
    fm = [f"id: {note.id}", f"type: {note.type}", f"title: {_scalar(note.title)}",
          f"aliases: {_flow_list(note.aliases)}", f"tags: {_flow_list(note.tags)}",
          f"created: {note.created}", f"updated: {note.updated}", f"importance: {note.importance}"]
    for k, v in note.extra.items():
        fm.append(f"{k}: {json.dumps(v, ensure_ascii=False, default=_iso)}")
    body = note.body if note.body.endswith("\n") else note.body + "\n"
    return "---\n" + "\n".join(fm) + "\n---\n\n" + body.lstrip("\n")


def loads(text: str, path: Path | None = None, default_type: str = "topic") -> Note:
    """Parse markdown (frontmatter optional) into a Note."""
    try:
        post = frontmatter.loads(text)
        meta, body = dict(post.metadata), post.content
    except Exception:  # noqa: BLE001 - malformed YAML must not make a note unreadable
        log.warning("bad frontmatter in %s; treating the file as body only", path)
        meta, body = {}, text
    title = str(meta.get("title") or "")
    if not title:
        h1 = re.search(r"^#\s+(.+)$", body, re.M)
        title = h1.group(1).strip() if h1 else (path.stem.replace("-", " ").title() if path else "Untitled")
    aliases = meta.get("aliases") or []
    tags = meta.get("tags") or []
    try:
        importance = max(1, min(5, int(meta.get("importance", 3))))
    except (TypeError, ValueError):
        importance = 3
    return Note(
        id=str(meta.get("id") or ""), type=str(meta.get("type") or default_type), title=title, body=body,
        aliases=[str(a) for a in ([aliases] if isinstance(aliases, str) else aliases)],
        tags=[str(t).lstrip("#") for t in ([tags] if isinstance(tags, str) else tags)],
        created=_iso(meta.get("created")), updated=_iso(meta.get("updated")), importance=importance,
        extra={k: v for k, v in meta.items() if k not in _KNOWN_KEYS}, path=path)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then replace. A crash leaves the original untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Vault:
    """Notes on disk. All mutations re-read the file right before writing so hand edits are never lost."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # ---- paths -----------------------------------------------------------------------------------------------
    def iter_paths(self):
        """All note files (*.md), skipping the hidden index folder."""
        for p in self.root.rglob("*.md"):
            if HIDDEN_DIR not in p.relative_to(self.root).parts:
                yield p

    def scan(self):
        """Yield (relative posix path, mtime_ns, size) for every note file, without opening any of them."""
        stack = [(self.root, "")]
        while stack:
            folder, prefix = stack.pop()
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            if e.name != HIDDEN_DIR:
                                stack.append((Path(e.path), f"{prefix}{e.name}/"))
                        elif e.name.endswith(".md") and not e.name.startswith("."):
                            st = e.stat()
                            yield f"{prefix}{e.name}", st.st_mtime_ns, st.st_size
            except OSError as exc:
                log.warning("cannot scan %s: %s", folder, exc)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def default_type(self, path: Path) -> str:
        r = path.relative_to(self.root)
        if len(r.parts) == 1:
            return FILE_TYPES.get(r.name, "topic")
        return FOLDER_TYPES.get(r.parts[0], "topic")

    def new_path(self, type_: str, title: str) -> Path:
        """Unused file path for a new note of this type."""
        if type_ in SINGLETONS:
            return self.root / SINGLETONS[type_]
        folder = self.root / TYPE_FOLDERS.get(type_, "topics")
        stem = parser.slugify(title)
        p, n = folder / f"{stem}.md", 2
        while p.exists():
            p = folder / f"{stem}-{n}.md"
            n += 1
        return p

    # ---- read / write ----------------------------------------------------------------------------------------
    def read(self, path: Path) -> Note:
        return loads(path.read_text(encoding="utf-8"), path, self.default_type(path))

    def write(self, note: Note) -> None:
        assert note.path is not None
        note.updated = now_iso()
        atomic_write(note.path, dumps(note))

    def create(self, type_: str, title: str, aliases: list[str] | None = None, tags: list[str] | None = None,
               body: str | None = None, importance: int = 3) -> Note:
        """Create and save a new note."""
        with self.lock:
            path = self.new_path(type_, title)
            ts = now_iso()
            note = Note(id=str(ULID()), type=type_, title=title, aliases=aliases or [], tags=tags or [],
                        created=ts, updated=ts, importance=importance, path=path,
                        body=body if body is not None else f"# {title}\n\n")
            atomic_write(path, dumps(note))
            return note

    @staticmethod
    def needs_identity(note: Note) -> bool:
        """True if the note has no id yet, or has hand-written bullets without block ids."""
        return not note.id or (note.type != "task-list" and parser.assign_missing_ids(note.body)[1])

    def ensure_identity(self, path: Path, note: Note | None = None) -> Note:
        """Read a note; if it lacks an id (hand-made file) or has bare bullets, fix that and save."""
        note = note or self.read(path)
        if self.needs_identity(note):
            with self.lock:
                note = self.read(path)
                if not note.id:
                    note.id = str(ULID())
                    note.created = note.created or now_iso()
                if note.type != "task-list":
                    note.body, _ = parser.assign_missing_ids(note.body)
                self.write(note)
                note = self.read(path)
        return note

    def mutate(self, path: Path, fn: Callable[[Note], None]) -> Note:
        """Apply `fn` to a fresh read of the note and save. Retries if the file changes under us."""
        with self.lock:
            for _attempt in range(3):
                before = path.stat()
                note = self.read(path)
                fn(note)
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                    self.write(note)
                    return note
            raise OSError(f"{path.name} keeps changing; try again")

    # ---- facts -----------------------------------------------------------------------------------------------
    @staticmethod
    def add_fact(note: Note, text: str, source: str, learned: str | None = None) -> str:
        """Append a fact bullet with provenance; returns its block id. Identical facts are not duplicated."""
        key = parser.normalize(parser.strip_links(text))
        for b in note.parsed().blocks:
            if parser.normalize(parser.strip_links(b.text)) == key:
                return b.block_id
        bid = parser.next_block_id(note.body)
        body = note.body if note.body.endswith("\n") else note.body + "\n"
        if body.rstrip("\n").splitlines()[-1:] and body.rstrip("\n").splitlines()[-1].startswith("#"):
            body += "\n"                                    # blank line between a heading and the first fact
        note.body = body + parser.format_fact(text.strip(), bid, source, learned or today())
        return bid

    @staticmethod
    def update_fact(note: Note, block_id: str, text: str, source: str, learned: str | None = None,
                    was: str | None = None) -> bool:
        """Replace a fact's text. `was` (the previous value) is recorded in the provenance comment as
        `was: <old>, updated <date>`, newest first, so changes stay traceable."""
        lines = note.body.split("\n")
        for b in note.parsed().blocks:
            if b.block_id == block_id:
                when = learned or today()
                lines[b.line] = re.sub(r"^(\s*[-*+]\s+).*$", lambda m: f"{m.group(1)}{text.strip()}  ^{block_id}",
                                       lines[b.line])
                history = b.history
                if was:
                    entry = f"was: {was.replace('-->', '').strip()}, updated {when}"
                    history = f"{entry}; {history}" if history else entry
                prov = f"  <!-- src: {source} · learned: {when}" + (f" · {history}" if history else "") + " -->"
                if b.line + 1 < len(lines) and parser.PROVENANCE_RE.match(lines[b.line + 1]):
                    lines[b.line + 1] = prov
                else:
                    lines.insert(b.line + 1, prov)
                note.body = "\n".join(lines)
                return True
        return False

    @staticmethod
    def replace_fact_text(note: Note, block_id: str, text: str) -> bool:
        """Change only a fact's text, leaving its provenance comment as it was."""
        lines = note.body.split("\n")
        for b in note.parsed().blocks:
            if b.block_id == block_id:
                lines[b.line] = re.sub(r"^(\s*[-*+]\s+).*$", lambda m: f"{m.group(1)}{text.strip()}  ^{block_id}",
                                       lines[b.line])
                note.body = "\n".join(lines)
                return True
        return False

    @staticmethod
    def delete_facts(note: Note, block_ids: set[str]) -> int:
        """Remove fact bullets (and their provenance lines) completely. Returns how many were removed."""
        lines = note.body.split("\n")
        drop: set[int] = set()
        removed = 0
        for b in note.parsed().blocks:
            if b.block_id in block_ids:
                removed += 1
                drop.add(b.line)
                if b.line + 1 < len(lines) and parser.PROVENANCE_RE.match(lines[b.line + 1]):
                    drop.add(b.line + 1)
        note.body = "\n".join(l for i, l in enumerate(lines) if i not in drop)
        return removed

    # ---- rename / delete -------------------------------------------------------------------------------------
    def rename(self, path: Path, new_title: str) -> Path:
        """Rename a note (title + filename) and repoint every [[link]] to it across the vault."""
        with self.lock:
            note = self.read(path)
            old_names = {parser.normalize(path.stem), parser.normalize(note.title)}
            if note.type in SINGLETONS:
                raise ValueError("profile, preferences and tasks can't be renamed")
            new_path = path if parser.slugify(new_title) == path.stem else self.new_path(note.type, new_title)
            note.title = new_title
            note.path = new_path
            self.write(note)
            if new_path != path:
                path.unlink()
            for p in self.iter_paths():
                text = p.read_text(encoding="utf-8")
                new = parser.rewrite_links(text, old_names, new_path.stem)
                if new != text:
                    atomic_write(p, new)
            return new_path

    def delete(self, path: Path) -> None:
        """Permanently remove a note file."""
        with self.lock:
            path.unlink(missing_ok=True)
