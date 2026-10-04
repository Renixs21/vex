"""Everything the memory viewer needs, as plain Python returning JSON-able dicts (the Qt window only forwards calls to this).

Reads come from the SQLite index; writes go through the vault's atomic writer, so a hand edit and the viewer never corrupt each other.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from memory import parser
from memory.api import Memory
from memory.secrets import find_secrets
from memory.vault import NOTE_TYPES, Vault, atomic_write, loads, now_iso

log = logging.getLogger("vex.memory.viewer")

HIDDEN_FROM_GRAPH = {"inbox"}
_PROPOSAL = re.compile(r"^(?P<fact>.*?) \((?P<label>proposed|proposed deletion) for (?P<note>.*?), (?P<type>[\w-]+), "
                       r"confidence (?P<conf>[0-9.]+); (?P<reason>.*)\)$")


class ViewerService:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.index = memory.index
        self.vault = memory.vault

    # ---- change detection ------------------------------------------------------------------------------------------
    def version(self) -> int:
        """Bumps whenever notes change (the window polls this and refreshes)."""
        self.index.sync()
        return self.index.version

    # ---- graph ---------------------------------------------------------------------------------------------------------
    def graph(self, show_daily: bool = False) -> dict[str, Any]:
        self.index.sync()
        rows = [r for r in self.index.list_notes() if r["type"] not in HIDDEN_FROM_GRAPH and (show_daily or r["type"] != "daily")]
        ids = {r["id"] for r in rows}
        degree: dict[str, int] = {i: 0 for i in ids}
        edges: set[tuple[str, str]] = set()
        ghosts: dict[str, str] = {}
        with self.index.lock:
            links = self.index.db.execute("SELECT src_note, target_note, target_raw FROM links").fetchall()
        for src, tgt, raw in links:
            if src not in ids:
                continue
            if tgt is None:
                gid = "ghost:" + parser.normalize(raw)
                ghosts[gid] = raw
                tgt = gid
            elif tgt not in ids or tgt == src:
                continue
            if (src, tgt) in edges or (tgt, src) in edges:
                continue
            edges.add((src, tgt))
            degree[src] = degree.get(src, 0) + 1
            degree[tgt] = degree.get(tgt, 0) + 1
        nodes = [{"id": r["id"], "title": r["title"], "type": r["type"], "links": degree.get(r["id"], 0),
                  "importance": r["importance"], "ghost": False} for r in rows]
        nodes += [{"id": g, "title": raw, "type": "ghost", "links": degree.get(g, 0), "importance": 1, "ghost": True}
                  for g, raw in ghosts.items()]
        return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}

    # ---- sidebar / search ----------------------------------------------------------------------------------------------
    def sidebar(self) -> dict[str, Any]:
        self.index.sync()
        types: dict[str, list[dict]] = {}
        recent = []
        for r in self.index.list_notes():
            if r["type"] == "inbox":
                continue
            types.setdefault(r["type"], []).append({"id": r["id"], "title": r["title"]})
            recent.append({"id": r["id"], "title": r["title"], "type": r["type"], "updated": r["updated"] or ""})
        recent.sort(key=lambda x: x["updated"], reverse=True)
        with self.index.lock:
            tags = [{"tag": t, "count": c} for t, c in self.index.db.execute(
                "SELECT tag, COUNT(*) FROM tags GROUP BY tag ORDER BY COUNT(*) DESC, tag LIMIT 60")]
        order = [t for t in ("profile", "preference", "person", "project", "topic", "place", "task-list", "daily") if t in types]
        return {"types": {t: types[t] for t in order + [t for t in types if t not in order]}, "tags": tags,
                "recent": recent[:12], "inbox": len(self.inbox())}

    def notes_with_tag(self, tag: str) -> list[dict]:
        return [{"id": r["id"], "title": r["title"], "type": r["type"]} for r in self.index.list_notes(None, tag)]

    def search(self, query: str, limit: int = 30) -> list[dict]:
        self.index.sync()
        q = " ".join((query or "").split())
        if not q:
            return []
        out: dict[str, dict] = {}
        qn = parser.normalize(q)
        for r in self.index.list_notes():
            if r["type"] == "inbox":
                continue
            names = self.index.names_of(r["id"])
            if qn in parser.normalize(r["title"]) or any(qn in n for n in names):
                out[r["id"]] = {"id": r["id"], "title": r["title"], "type": r["type"], "snippet": ""}
        terms = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
        for b in self.index.search_blocks(terms, limit=limit, match_all=False):
            nid = b["note_id"]
            info = self.index.note(nid)
            if info is None or info["type"] == "inbox":
                continue
            out.setdefault(nid, {"id": nid, "title": info["title"], "type": info["type"],
                                 "snippet": parser.strip_links(b["text"])[:140]})
        return list(out.values())[:limit]

    # ---- notes ---------------------------------------------------------------------------------------------------------------
    def resolve(self, name: str) -> str | None:
        """Note id a [[link]] target points to, or None (a ghost link)."""
        row = self.index.resolve(name)
        return row["id"] if row else None

    def note(self, note_id: str) -> dict[str, Any] | None:
        self.index.sync()
        row = self.index.note(note_id)
        if row is None:
            return None
        path = self.vault.root / row["path"]
        note = self.vault.read(path)
        parsed = note.parsed()
        back = []
        for b in self.index.blocks_linking_to([note_id]):
            src = self.index.note(b["note_id"])
            if src is None or src["id"] == note_id:
                continue
            back.append({"id": src["id"], "title": src["title"], "type": src["type"], "text": parser.strip_links(b["text"])})
        out_ids, _ = self.index.neighbors(note_id)
        self.index.touch([note_id], now_iso())
        return {"id": note_id, "title": note.title, "type": note.type, "path": row["path"], "importance": note.importance,
                "aliases": note.aliases, "tags": sorted({*note.tags, *parsed.tags}), "created": note.created, "updated": note.updated,
                "body": note.body, "backlinks": back,
                "facts": [{"id": b.block_id, "text": b.text, "source": b.source, "learned": b.learned, "history": b.history}
                          for b in parsed.blocks],
                "links_out": [{"id": i, "title": (self.index.note(i) or {"title": "?"})["title"]} for i in out_ids]}

    def raw(self, note_id: str) -> str | None:
        path = self.index.path_of(note_id)
        return path.read_text(encoding="utf-8") if path else None

    def save(self, note_id: str, text: str) -> dict[str, Any]:
        """Replace a note's file with the edited text (validated, atomic). Never saves a secret."""
        path = self.index.path_of(note_id)
        if path is None:
            return {"ok": False, "error": "That note no longer exists."}
        if kinds := find_secrets(text):
            return {"ok": False, "error": f"Not saved: the text looks like it contains a secret ({', '.join(kinds)})."}
        try:
            parsed = loads(text, path, self.vault.default_type(path))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Couldn't read that as a note: {e}"}
        if parsed.id and parsed.id != note_id:
            return {"ok": False, "error": "Don't change the id line in the header; it keeps the note's identity."}
        with self.vault.lock:
            atomic_write(path, text)
        self.index.sync()
        return {"ok": True}

    # ---- inbox -------------------------------------------------------------------------------------------------------------------
    def inbox(self) -> list[dict[str, Any]]:
        self.index.sync()
        items = []
        for row in self.index.list_notes("inbox"):
            note = self.vault.read(self.vault.root / row["path"])
            for b in note.parsed().blocks:
                m = _PROPOSAL.match(b.text.strip())
                item = {"path": row["path"], "block": b.block_id, "text": b.text.strip(), "learned": b.learned,
                        "fact": b.text.strip(), "note": "", "type": "topic", "confidence": None, "reason": "", "deletion": False}
                if m:
                    item.update(fact=m["fact"].strip(), note=m["note"], type=m["type"] if m["type"] in NOTE_TYPES else "topic",
                                confidence=float(m["conf"]), reason=m["reason"], deletion=m["label"] == "proposed deletion")
                items.append(item)
        items.sort(key=lambda i: i["learned"], reverse=True)
        return items

    def _drop_inbox_block(self, path: str, block: str) -> bool:
        p = self.vault.root / path
        if not p.exists():
            return False
        done: dict[str, int] = {}
        self.vault.mutate(p, lambda n: done.update(n=Vault.delete_facts(n, {block})))
        self.index.sync()
        return bool(done.get("n"))

    def approve(self, path: str, block: str, note: str = "", note_type: str = "") -> dict[str, Any]:
        """Save an inbox fact into its note (the name and type can be corrected first), then remove it from the inbox."""
        item = next((i for i in self.inbox() if i["path"] == path and i["block"] == block), None)
        if item is None:
            return {"ok": False, "error": "That inbox item is gone."}
        if item["deletion"]:
            return {"ok": False, "error": "Deletions are never applied from here. Say 'forget ...' to Vex, or discard this."}
        target = (note or item["note"]).strip()
        ntype = (note_type or item["type"]).strip()
        res = self.memory.remember(item["fact"], about=target or None, note_type=ntype if ntype in NOTE_TYPES else None,
                                   source="approved from inbox")
        if not res.ok:
            return {"ok": False, "error": res.message}
        self._drop_inbox_block(path, block)
        return {"ok": True, "message": res.message}

    def discard(self, path: str, block: str) -> dict[str, Any]:
        return {"ok": self._drop_inbox_block(path, block)}
