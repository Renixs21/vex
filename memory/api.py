"""Plain Python functions behind the memory tools (remember, recall, read_note, ...). Not yet registered with the agent.

Every method returns a `Result` and never raises for expected problems, matching the tool-result shape used elsewhere
(`ok`, `message`, optional `data`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from memory import forget as forget_mod
from memory import parser
from memory.index import Index
from memory.retrieve import STOPWORDS, ContextPack, retrieve, terms
from memory.secrets import find_secrets
from memory.vault import NOTE_TYPES, SINGLETONS, Note, Vault, now_iso, today

log = logging.getLogger("vex.memory")

SELF_NAMES = {"me", "myself", "i", "user", "self", "my profile"}
UPDATE_SIMILARITY = 90          # >= this (0-100) means "same fact, reworded": update instead of adding


def _only_numbers_differ(a: str, b: str) -> bool:
    """True when two facts have the same words except for numbers/dates ('on the 20th' vs 'on the 21st')."""
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb) or len(ta) < 3:
        return False
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    return bool(diff) and all(re.search(r"\d", x) and re.search(r"\d", y) for x, y in diff)


def _previous_value(old: str, new: str) -> str:
    """What changed, for the provenance comment: the old number/date if only that differs, else the old text."""
    ot, nt = parser.strip_links(old).split(), parser.strip_links(new).split()
    if len(ot) == len(nt):
        diff = [o for o, n in zip(ot, nt) if o != n]
        if diff and all(re.search(r"\d", d) for d in diff):
            return " ".join(diff)
    old = " ".join(ot)
    return old if len(old) <= 80 else old[:77] + "..."


@dataclass
class Result:
    ok: bool
    message: str
    data: Any = None
    needs_confirmation: bool = False


class Memory:
    """VexMind facade: one vault + its index."""

    def __init__(self, root: Path | None = None, watch: bool = False):
        if root is None:
            from config import ROOT, settings
            root = ROOT / settings["paths"]["vault"]
        self.vault = Vault(Path(root))
        self.index = Index(self.vault)
        self.index.sync()
        if watch:
            self.index.start_watching()

    def close(self) -> None:
        self.index.close()

    def reindex(self) -> Result:
        r = self.index.rebuild()
        s = self.index.stats()
        return Result(True, f"Reindexed {s['notes']} notes, {s['facts']} facts.", {**r, **s})

    # ---- remember --------------------------------------------------------------------------------------------
    def remember(self, fact: str, about: str | None = None, tags: list[str] | None = None,
                 note_type: str | None = None, source: str = "user", aliases: list[str] | None = None) -> Result:
        """Save a durable fact in the right note (creating it if needed). Refuses secrets."""
        fact = " ".join(fact.split())
        if not fact:
            return Result(False, "Nothing to remember.")
        if kinds := find_secrets(fact):
            return Result(False, f"Not saving that: it looks like a secret ({', '.join(kinds)}).")
        if note_type and note_type not in NOTE_TYPES:
            return Result(False, f"Unknown note type '{note_type}'. Use one of: {', '.join(NOTE_TYPES)}.")
        self.index.sync()
        try:
            path = self._target_note(fact, about, note_type)
            target = self.vault.read(path)
            text = self._autolink(fact, target)
            outcome: dict[str, str] = {}
            clean_tags = [t.lstrip("#").casefold() for t in (tags or []) if t.strip()]

            def apply(note: Note) -> None:
                existing = note.parsed().blocks
                key = parser.normalize(parser.strip_links(text))
                best = max(existing, key=lambda b: fuzz.ratio(key, parser.normalize(parser.strip_links(b.text))),
                           default=None)
                best_key = parser.normalize(parser.strip_links(best.text)) if best else ""
                score = fuzz.ratio(key, best_key) if best else 0
                if best and score == 100:
                    outcome.update(action="duplicate", block_id=best.block_id)
                elif best and (score >= UPDATE_SIMILARITY or _only_numbers_differ(key, best_key)):
                    Vault.update_fact(note, best.block_id, text, source, was=_previous_value(best.text, text))
                    outcome.update(action="updated", block_id=best.block_id)
                else:
                    outcome.update(action="added", block_id=Vault.add_fact(note, text, source))
                for t in clean_tags:
                    if t not in note.tags:
                        note.tags.append(t)
                for a in aliases or []:
                    if a.strip() and parser.normalize(a) not in note.names():
                        note.aliases.append(a.strip())

            note = self.vault.mutate(path, apply)
            self.index.sync()
        except (OSError, ValueError) as e:
            log.exception("remember failed")
            return Result(False, f"Couldn't save that: {e}")
        verb = {"added": "Remembered", "updated": "Updated", "duplicate": "Already knew"}[outcome["action"]]
        return Result(True, f"{verb}: [{note.title}] {parser.strip_links(text)}",
                      {"note": note.title, "path": self.vault.rel(path), **outcome})

    def _target_note(self, fact: str, about: str | None, note_type: str | None) -> Path:
        """Pick (or create) the note a fact belongs in."""
        if note_type in SINGLETONS and not about:
            return self._singleton(note_type)
        if about:
            if parser.normalize(about) in SELF_NAMES:
                return self._singleton("profile")
            row = self.index.resolve(about)
            if row:
                return self.vault.root / row["path"]
            return self.vault.create(note_type if note_type not in SINGLETONS else "topic", about.strip()).path  # type: ignore[return-value]
        ents = self.index.find_entities(fact, fuzzy=False, stopwords=STOPWORDS)
        if ents and ents[0][2] >= 1.0:
            return self.vault.root / self.index.note(ents[0][0])["path"]
        inbox = self.vault.root / "inbox" / f"{today()}.md"
        if not inbox.exists():
            self.vault.create("inbox", today()).path
        return inbox

    def _singleton(self, type_: str) -> Path:
        rows = self.index.list_notes(type_)
        if rows:
            return self.vault.root / rows[0]["path"]
        title = {"profile": "Profile", "preference": "Preferences", "task-list": "Tasks"}[type_]
        return self.vault.create(type_, title).path  # type: ignore[return-value]

    def _autolink(self, text: str, target: Note) -> str:
        """Wrap the first mention of each other known note in [[wikilinks]] so facts connect to each other."""
        for nid, phrase, score in self.index.find_entities(text, fuzzy=False, stopwords=STOPWORDS):
            if score < 1.0 or nid == target.id:
                continue
            row = self.index.note(nid)
            if row is None or row["type"] in ("daily", "inbox"):
                continue
            words = [re.escape(w) for w in phrase.split()]
            rx = re.compile(r"(?<![\w\[])" + r"[\s_-]+".join(words) + r"(?![\w\]])", re.I)
            for m in rx.finditer(text):
                if text.count("[[", 0, m.start()) > text.count("]]", 0, m.start()):
                    continue                                    # already inside a wikilink
                stem = Path(row["path"]).stem
                shown = m.group(0)
                same = parser.normalize(shown) in (parser.normalize(stem), parser.normalize(row["title"]))
                link = f"[[{shown}]]" if same else f"[[{stem}|{shown}]]"
                text = text[:m.start()] + link + text[m.end():]
                break
        return text

    # ---- recall / read ---------------------------------------------------------------------------------------
    def retrieve(self, query: str, **kw) -> ContextPack:
        """Raw context pack for the agent's prompt (the index must be in sync)."""
        return retrieve(self.index, query, **kw)

    def recall(self, query: str) -> Result:
        """Facts relevant to a question."""
        self.index.sync()
        pack = retrieve(self.index, query)
        if not pack.items:
            return Result(True, "Nothing relevant in memory.", [])
        return Result(True, "\n".join(i.line() for i in pack.items),
                      [{"note": i.title, "block": i.block_id, "text": parser.strip_links(i.text),
                        "score": round(i.score, 3)} for i in pack.items])

    def read_note(self, title: str) -> Result:
        self.index.sync()
        row = self.index.resolve(title)
        if not row:
            return Result(False, f"No note called '{title}'.")
        text = (self.vault.root / row["path"]).read_text(encoding="utf-8")
        return Result(True, text[:8000], {"path": row["path"], "truncated": len(text) > 8000})

    def list_notes(self, type_: str | None = None, tag: str | None = None) -> Result:
        self.index.sync()
        rows = self.index.list_notes(type_, tag)
        return Result(True, "\n".join(f"{r['type']}: {r['title']}" for r in rows) or "No notes.",
                      [{"title": r["title"], "type": r["type"], "path": r["path"]} for r in rows])

    def memory_stats(self) -> Result:
        self.index.sync()
        s = self.index.stats()
        msg = (f"{s['notes']} notes, {s['facts']} facts, {s['links']} links "
               f"({s['ghost_links']} unresolved), {s['tags']} tags.")
        return Result(True, msg, s)

    # ---- link / rename ---------------------------------------------------------------------------------------
    def link_notes(self, a: str, b: str, reason: str | None = None) -> Result:
        """Add a link from note `a` to note `b` (a fact in `a` pointing at `b`)."""
        self.index.sync()
        ra, rb = self.index.resolve(a), self.index.resolve(b)
        for name, row in ((a, ra), (b, rb)):
            if row is None:
                return Result(False, f"No note called '{name}'.")
        if find_secrets(reason or ""):
            return Result(False, "Not saving that: the reason looks like a secret.")
        text = f"Related to [[{Path(rb['path']).stem}|{rb['title']}]]" + (f": {reason.strip()}" if reason else "")
        self.vault.mutate(self.vault.root / ra["path"], lambda n: Vault.add_fact(n, text, "link"))
        self.index.sync()
        return Result(True, f"Linked [{ra['title']}] to [{rb['title']}].")

    def rename_note(self, old: str, new_title: str) -> Result:
        """Rename a note and update every [[link]] pointing at it."""
        self.index.sync()
        row = self.index.resolve(old)
        if not row:
            return Result(False, f"No note called '{old}'.")
        try:
            new_path = self.vault.rename(self.vault.root / row["path"], new_title)
        except (OSError, ValueError) as e:
            return Result(False, f"Couldn't rename: {e}")
        self.index.sync()
        return Result(True, f"Renamed '{row['title']}' to '{new_title}'.", {"path": self.vault.rel(new_path)})

    # ---- forget ----------------------------------------------------------------------------------------------
    def forget(self, query: str, confirm: bool = False, mentions: forget_mod.MentionAction = "redact",
               choices: dict[tuple[str, str], forget_mod.MentionAction] | None = None) -> Result:
        """Forget facts or a whole note. Without confirm=True this only previews.

        Whole-note forgetting: the note goes to the Recycle Bin, facts mainly about it are deleted, and facts that merely
        mention it have just the name removed (`mentions="redact"`, the default) or are deleted (`mentions="delete"`);
        `choices` overrides that per fact, keyed by (note_id, block_id). Only titles and counts are logged.
        """
        self.index.sync()
        plan = forget_mod.build_plan(self.index, query)
        if plan.empty:
            return Result(True, f"I don't have anything matching '{query}'.")
        if not confirm:
            return Result(True, forget_mod.preview(plan), plan.as_dict(), needs_confirmation=True)
        out = forget_mod.execute(self.index, self.vault, plan, default=mentions, choices=choices)
        bits = []
        if out["notes"]:
            bits.append(f"moved {len(out['notes'])} note(s) to the Recycle Bin")
        if out["deleted"]:
            bits.append(f"deleted {out['deleted']} fact(s)")
        if out["redacted"]:
            bits.append(f"removed the name from {out['redacted']} fact(s)")
        msg = "Forgot: " + (", ".join(bits) or "nothing changed") + "."
        if out["errors"]:
            return Result(False, msg + " Problems: " + "; ".join(out["errors"]), out)
        return Result(True, msg, out)
