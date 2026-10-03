"""SQLite + FTS5 index over the vault. Always rebuildable from the markdown files."""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from pathlib import Path

from rapidfuzz import fuzz, process
from ulid import ULID

from memory import parser
from memory.vault import HIDDEN_DIR, Note, Vault, loads

log = logging.getLogger("vex.memory.index")

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT, type TEXT, importance INTEGER,
    created TEXT, updated TEXT, mtime INTEGER, size INTEGER, hash TEXT);
CREATE TABLE IF NOT EXISTS aliases(alias_norm TEXT NOT NULL, note_id TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY(alias_norm, note_id));
CREATE TABLE IF NOT EXISTS blocks(note_id TEXT NOT NULL, block_id TEXT NOT NULL, text TEXT, learned TEXT, source TEXT,
    PRIMARY KEY(note_id, block_id));
CREATE TABLE IF NOT EXISTS links(src_note TEXT NOT NULL, src_block TEXT, target_note TEXT, target_raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_note);
CREATE INDEX IF NOT EXISTS idx_links_tgt ON links(target_note);
CREATE TABLE IF NOT EXISTS tags(note_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(note_id, tag));
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(text, title, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS access(note_id TEXT PRIMARY KEY, last_used TEXT, use_count INTEGER NOT NULL DEFAULT 0);
"""


def _file_sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


class Index:
    """Index of one vault. Thread-safe (one connection guarded by a lock)."""

    def __init__(self, vault: Vault, db_path: Path | None = None):
        self.vault = vault
        hidden = vault.root / HIDDEN_DIR
        hidden.mkdir(exist_ok=True)
        self.db_path = db_path or hidden / "index.db"
        self.lock = threading.RLock()
        self.version = 0                       # bumps whenever the index content changes
        self._alias_cache: tuple[int, list[str]] | None = None
        self._open()
        self._observer = None
        self._timer: threading.Timer | None = None

    # ---- connection ------------------------------------------------------------------------------------------
    def _open(self) -> None:
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("vnorm", 1, lambda s: parser.normalize(s or ""), deterministic=True)
        try:
            self.db.executescript(SCHEMA)
            row = self.db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if row is not None and row["v"] != SCHEMA_VERSION:
                raise sqlite3.DatabaseError("schema version changed")
        except sqlite3.DatabaseError:
            log.warning("index unreadable or outdated; rebuilding from the markdown files")
            self.db.close()
            self.db_path.unlink(missing_ok=True)
            self.db = sqlite3.connect(self.db_path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.create_function("vnorm", 1, lambda s: parser.normalize(s or ""), deterministic=True)
            self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")      # the index is rebuildable, so favour speed over durability
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    def close(self) -> None:
        self.stop_watching()
        with self.lock:
            self.db.close()

    # ---- sync ------------------------------------------------------------------------------------------------
    def sync(self) -> dict[str, int]:
        """Bring the index in line with the files. Only changed files are re-read and re-indexed."""
        with self.lock:
            known = {r["path"]: r for r in self.db.execute("SELECT id, path, mtime, size, hash FROM notes")}
            id_path = {r["id"]: r["path"] for r in known.values()}
            seen_paths: set[str] = set()
            seen_ids: set[str] = set()
            changed = 0
            for rel, mtime, size in self.vault.scan():
                p = self.vault.root / rel
                seen_paths.add(rel)
                try:
                    row = known.get(rel)
                    if row is not None and (row["mtime"], row["size"]) == (mtime, size):
                        seen_ids.add(row["id"])
                        continue
                    raw = p.read_bytes()                       # one read per changed file
                    note = loads(raw.decode("utf-8", errors="replace"), p, self.vault.default_type(p))
                    if self.vault.needs_identity(note):
                        note = self.vault.ensure_identity(p, note)
                        raw = p.read_bytes()
                    note.path = p
                    other = id_path.get(note.id)
                    copied = other is not None and other != rel and (self.vault.root / other).exists()
                    if note.id in seen_ids or copied:            # a copied file: give it its own identity
                        self.vault.mutate(p, lambda n: setattr(n, "id", str(ULID())))
                        note = self.vault.read(p)
                        raw = p.read_bytes()
                    sig = _file_sig(p)
                    digest = hashlib.sha1(raw).hexdigest()
                    if row is not None and row["hash"] == digest and row["id"] == note.id:
                        self.db.execute("UPDATE notes SET mtime=?, size=? WHERE id=?", (*sig, note.id))
                    else:
                        self._index_note(note, rel, sig, digest)
                        changed += 1
                    seen_ids.add(note.id)
                except (OSError, sqlite3.Error) as e:
                    log.warning("could not index %s: %s", p, e)
            gone = [r["id"] for r in self.db.execute("SELECT id FROM notes") if r["id"] not in seen_ids]
            for nid in gone:
                self._delete_rows(nid)
            if changed or gone:
                self._resolve_links()
                self.version += 1
            self.db.commit()
            return {"changed": changed, "removed": len(gone)}

    def rebuild(self) -> dict[str, int]:
        """Throw the index away and rebuild it from the files (`reindex`)."""
        with self.lock:
            for t in ("notes", "aliases", "blocks", "links", "tags", "blocks_fts"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.commit()
            return self.sync()

    def _delete_rows(self, note_id: str) -> None:
        rows = [r[0] for r in self.db.execute("SELECT rowid FROM blocks WHERE note_id=?", (note_id,))]
        self.db.executemany("DELETE FROM blocks_fts WHERE rowid=?", [(r,) for r in rows])
        for t, col in (("blocks", "note_id"), ("links", "src_note"), ("tags", "note_id"), ("aliases", "note_id"),
                       ("access", "note_id"), ("notes", "id")):
            self.db.execute(f"DELETE FROM {t} WHERE {col}=?", (note_id,))

    def _index_note(self, note: Note, rel: str, sig: tuple[int, int], digest: str) -> None:
        self._delete_rows(note.id)
        stale = self.db.execute("SELECT id FROM notes WHERE path=?", (rel,)).fetchone()
        if stale:                                   # a different note used to live at this path
            self._delete_rows(stale["id"])
        self.db.execute("INSERT INTO notes(id, path, title, type, importance, created, updated, mtime, size, hash) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (note.id, rel, note.title, note.type, note.importance, note.created, note.updated, *sig, digest))
        stem = Path(rel).stem
        names = [(parser.normalize(stem), 0), (parser.normalize(note.title), 1)]
        names += [(parser.normalize(a), 2) for a in note.aliases]
        for name, rank in names:
            if name:
                self.db.execute("INSERT INTO aliases VALUES(?,?,?) ON CONFLICT(alias_norm, note_id) "
                                "DO UPDATE SET rank=min(rank, excluded.rank)", (name, note.id, rank))
        parsed = note.parsed()
        if note.type != "task-list":
            for b in parsed.blocks:
                cur = self.db.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?,?)",
                                      (note.id, b.block_id, b.text, b.learned, b.source))
                self.db.execute("INSERT INTO blocks_fts(rowid, text, title) VALUES(?,?,?)",
                                (cur.lastrowid, parser.strip_links(b.text), note.title))
        for raw, blk in parsed.links:
            self.db.execute("INSERT INTO links(src_note, src_block, target_note, target_raw) VALUES(?,?,NULL,?)",
                            (note.id, blk, raw))
        for tag in {*(t.casefold() for t in note.tags), *parsed.tags}:
            self.db.execute("INSERT OR IGNORE INTO tags VALUES(?,?)", (note.id, tag))

    def _resolve_links(self) -> None:
        """Point each link at the note its target name resolves to (NULL = ghost link)."""
        self.db.execute(
            "UPDATE links SET target_note = (SELECT a.note_id FROM aliases a WHERE a.alias_norm = vnorm(links.target_raw) "
            "ORDER BY a.rank LIMIT 1)")

    # ---- watcher ---------------------------------------------------------------------------------------------
    def start_watching(self, debounce: float = 0.4) -> None:
        """Re-sync shortly after files in the vault change (hand edits show up within a second)."""
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        if self._observer is not None:
            return
        index = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                src = Path(str(event.src_path))
                if HIDDEN_DIR in src.parts or src.name.endswith(".tmp"):
                    return
                index._schedule_sync(debounce)

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.vault.root), recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def _schedule_sync(self, delay: float) -> None:
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._safe_sync)
            self._timer.daemon = True
            self._timer.start()

    def _safe_sync(self) -> None:
        try:
            self.sync()
        except Exception:  # noqa: BLE001 - a watcher thread must never die
            log.exception("background sync failed")

    def stop_watching(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    # ---- lookups ---------------------------------------------------------------------------------------------
    def resolve(self, name: str) -> sqlite3.Row | None:
        """Note whose filename, title or alias matches `name` (normalized, exact)."""
        with self.lock:
            return self.db.execute(
                "SELECT n.* FROM aliases a JOIN notes n ON n.id=a.note_id WHERE a.alias_norm=? "
                "ORDER BY a.rank, n.importance DESC LIMIT 1", (parser.normalize(name),)).fetchone()

    def note(self, note_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()

    def path_of(self, note_id: str) -> Path | None:
        row = self.note(note_id)
        return self.vault.root / row["path"] if row else None

    def alias_names(self) -> list[str]:
        """All normalized names, cached until the index changes (for fuzzy entity matching)."""
        with self.lock:
            if self._alias_cache is None or self._alias_cache[0] != self.version:
                names = [r[0] for r in self.db.execute("SELECT DISTINCT alias_norm FROM aliases")]
                self._alias_cache = (self.version, names)
            return self._alias_cache[1]

    def names_of(self, note_id: str) -> set[str]:
        """Every normalized name a note answers to (filename stem, title, aliases)."""
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT alias_norm FROM aliases WHERE note_id=?", (note_id,))}

    def notes_for_name(self, name_norm: str) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT note_id FROM aliases WHERE alias_norm=? ORDER BY rank", (name_norm,))]

    def find_entities(self, text: str, fuzzy: bool = True, stopwords: frozenset[str] = frozenset()) -> list[tuple[str, str, float]]:
        """Notes whose names appear in `text`: [(note_id, matched words, score 0-1)], best first.

        Exact n-gram matches (up to 4 words) score 1.0; fuzzy single-word matches score by similarity.
        """
        words = parser.normalize(re.sub(r"['’]s\b", "", text)).split()
        hits: dict[str, tuple[str, float]] = {}
        names = self.alias_names()
        nameset = set(names)
        for n in (4, 3, 2, 1):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                if n == 1 and (gram[0] in stopwords or len(gram[0]) < 2):
                    continue
                phrase = " ".join(gram)
                if phrase in nameset:
                    for nid in self.notes_for_name(phrase):
                        if nid not in hits or hits[nid][1] < 1.0:
                            hits[nid] = (phrase, 1.0)
                elif fuzzy and n == 1 and len(phrase) >= 4 and phrase not in stopwords:
                    m = process.extractOne(phrase, names, scorer=fuzz.ratio, score_cutoff=88)
                    if m:
                        for nid in self.notes_for_name(m[0]):
                            if nid not in hits:
                                hits[nid] = (phrase, m[1] / 100 * 0.9)
        return sorted(((nid, ph, sc) for nid, (ph, sc) in hits.items()), key=lambda t: -t[2])

    def search_blocks(self, terms: list[str], limit: int = 30, match_all: bool = False) -> list[sqlite3.Row]:
        """BM25-ranked fact blocks matching any term (or all of them). Lower `score` is better (SQLite convention)."""
        clean = [t.replace('"', "") for t in terms if t.strip()]
        if not clean:
            return []
        query = (" AND " if match_all else " OR ").join(f'"{t}"' for t in clean)
        with self.lock:
            try:
                return self.db.execute(
                    "SELECT b.note_id, b.block_id, b.text, b.learned, bm25(blocks_fts, 1.0, 0.6) AS score "
                    "FROM blocks_fts JOIN blocks b ON b.rowid = blocks_fts.rowid "
                    "WHERE blocks_fts MATCH ? ORDER BY score LIMIT ?", (query, limit)).fetchall()
            except sqlite3.OperationalError as e:
                log.warning("search failed for %r: %s", query, e)
                return []

    def blocks_of(self, note_ids: list[str]) -> list[sqlite3.Row]:
        if not note_ids:
            return []
        marks = ",".join("?" * len(note_ids))
        with self.lock:
            return self.db.execute(
                f"SELECT note_id, block_id, text, learned FROM blocks WHERE note_id IN ({marks}) ORDER BY rowid",
                note_ids).fetchall()

    def neighbors(self, note_id: str) -> tuple[list[str], list[str]]:
        """(outgoing targets, backlink sources) for a note, as note ids. Ghost links are skipped."""
        with self.lock:
            out = [r[0] for r in self.db.execute(
                "SELECT DISTINCT target_note FROM links WHERE src_note=? AND target_note IS NOT NULL AND target_note<>?",
                (note_id, note_id))]
            back = [r[0] for r in self.db.execute(
                "SELECT DISTINCT src_note FROM links WHERE target_note=? AND src_note<>?", (note_id, note_id))]
        return out, back

    def blocks_linking_to(self, note_ids: list[str]) -> list[sqlite3.Row]:
        """Fact blocks (in any note) that contain a link to one of `note_ids`."""
        if not note_ids:
            return []
        marks = ",".join("?" * len(note_ids))
        with self.lock:
            return self.db.execute(
                f"SELECT DISTINCT b.note_id, b.block_id, b.text, b.learned, l.target_note FROM links l "
                f"JOIN blocks b ON b.note_id=l.src_note AND b.block_id=l.src_block "
                f"WHERE l.target_note IN ({marks})", note_ids).fetchall()

    def note_info(self, note_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not note_ids:
            return {}
        marks = ",".join("?" * len(note_ids))
        with self.lock:
            rows = self.db.execute(f"SELECT n.*, COALESCE(a.use_count,0) AS use_count, a.last_used FROM notes n "
                                   f"LEFT JOIN access a ON a.note_id=n.id WHERE n.id IN ({marks})", note_ids)
            return {r["id"]: r for r in rows}

    def touch(self, note_ids: list[str], when: str) -> None:
        """Record that these notes were used (feeds the recency/importance boost)."""
        with self.lock:
            self.db.executemany(
                "INSERT INTO access(note_id, last_used, use_count) VALUES(?,?,1) "
                "ON CONFLICT(note_id) DO UPDATE SET last_used=excluded.last_used, use_count=use_count+1",
                [(n, when) for n in note_ids])
            self.db.commit()

    def list_notes(self, type_: str | None = None, tag: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT DISTINCT n.* FROM notes n", []
        if tag:
            sql += " JOIN tags t ON t.note_id=n.id"
        where = []
        if type_:
            where.append("n.type=?")
            args.append(type_)
        if tag:
            where.append("t.tag=?")
            args.append(tag.lstrip("#").casefold())
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.lock:
            return self.db.execute(sql + " ORDER BY n.type, n.title COLLATE NOCASE", args).fetchall()

    def stats(self) -> dict:
        with self.lock:
            q = lambda s: self.db.execute(s).fetchone()[0]  # noqa: E731
            by_type = {r[0]: r[1] for r in self.db.execute("SELECT type, COUNT(*) FROM notes GROUP BY type")}
            recent = [dict(r) for r in self.db.execute("SELECT title, type, path, updated FROM notes ORDER BY updated DESC LIMIT 5")]
            return {"notes": q("SELECT COUNT(*) FROM notes"), "facts": q("SELECT COUNT(*) FROM blocks"),
                    "links": q("SELECT COUNT(*) FROM links"), "ghost_links": q("SELECT COUNT(*) FROM links WHERE target_note IS NULL"),
                    "tags": q("SELECT COUNT(DISTINCT tag) FROM tags"), "by_type": by_type, "recent": recent}

    def dump(self) -> dict:
        """Deterministic snapshot of indexed content (used to compare incremental sync with a full rebuild)."""
        with self.lock:
            return {
                "notes": [tuple(r) for r in self.db.execute("SELECT id, path, title, type, importance FROM notes ORDER BY id")],
                "aliases": [tuple(r) for r in self.db.execute("SELECT alias_norm, note_id, rank FROM aliases ORDER BY 1,2")],
                "blocks": [tuple(r) for r in self.db.execute("SELECT note_id, block_id, text, learned, source FROM blocks ORDER BY 1,2")],
                "links": sorted(tuple(r) for r in self.db.execute("SELECT src_note, IFNULL(src_block,''), IFNULL(target_note,''), target_raw FROM links")),
                "tags": [tuple(r) for r in self.db.execute("SELECT note_id, tag FROM tags ORDER BY 1,2")],
            }
