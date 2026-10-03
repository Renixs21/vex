"""Forgetting: build a plan, preview it, carry it out.

Whole-note forgetting ("forget everything about Rahul") splits into three groups:
  a) the person's own note        -> Recycle Bin (never a permanent delete)
  b) facts mainly about them      -> deleted
  c) facts that merely mention them next to other things (team lists, "X works with Rahul")
                                  -> by default only their name/link is removed; the user may choose delete per fact

Nothing in this module logs forgotten text: only note titles and counts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from send2trash import send2trash

from memory import parser
from memory.index import Index
from memory.retrieve import STOPWORDS, terms
from memory.vault import SINGLETONS, Vault

log = logging.getLogger("vex.memory.forget")

FILLER = {"everything", "anything", "all", "about", "regarding", "on", "of", "info", "information", "forget",
          "delete", "remove", "memory", "memories", "what", "you", "know", "the", "my", "from", "stuff",
          "things", "that", "me", "please"}
_P = "\x00"                       # placeholder for a removed name
MentionAction = Literal["redact", "delete"]


@dataclass
class Item:
    note_id: str
    block_id: str
    title: str
    text: str
    new_text: str | None = None   # for mentions: the fact with the name removed (None = nothing left, must delete)

    @property
    def key(self) -> tuple[str, str]:
        return self.note_id, self.block_id


@dataclass
class Plan:
    notes: list[tuple[str, str, str]] = field(default_factory=list)   # (id, title, relative path): to Recycle Bin
    note_facts: int = 0                                                # facts inside those notes (go with the file)
    subject: list[Item] = field(default_factory=list)                  # b) delete
    mentions: list[Item] = field(default_factory=list)                 # c) redact by default
    facts: list[Item] = field(default_factory=list)                    # fact-level query matches: delete

    @property
    def empty(self) -> bool:
        return not (self.notes or self.subject or self.mentions or self.facts)

    def as_dict(self) -> dict:
        row = lambda i: {"note_id": i.note_id, "block_id": i.block_id, "note": i.title, "text": i.text,  # noqa: E731
                         "new_text": i.new_text}
        return {"notes": [t for _i, t, _p in self.notes], "note_facts": self.note_facts,
                "subject": [row(i) for i in self.subject], "mentions": [row(i) for i in self.mentions],
                "facts": [row(i) for i in self.facts]}


# ---- name handling ------------------------------------------------------------------------------------------------
def name_regex(names: set[str]) -> re.Pattern:
    alts = "|".join(r"[\s_-]+".join(re.escape(w) for w in n.split()) for n in sorted(names, key=len, reverse=True) if n)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.I)


def mentions_name(text: str, rx: re.Pattern) -> bool:
    return bool(rx.search(parser.strip_links(text)))


def is_subject(text: str, rx: re.Pattern) -> bool:
    """True when the fact starts with the person's name as its (sole) subject: 'Rahul is helping...'.

    'Rahul and Priya...', 'Rahul, Priya...' or a name later in the sentence are mentions, not subjects.
    """
    t = parser.strip_links(text).strip()
    m = rx.match(t)
    if not m:
        return False
    rest = re.sub(r"^['’]s", "", t[m.end():]).lstrip()
    return not re.match(r"(,|&|/|\+|and\b|or\b|with\b)", rest, re.I)


def redact(text: str, names: set[str]) -> str | None:
    """`text` with the person's name/links removed, or None if nothing meaningful is left.

    'Team: [[rahul|Rahul]], Priya' -> 'Team: Priya';  'Priya works with Rahul' -> 'Priya works with someone'.
    """
    rx = name_regex(names)

    def link(m: re.Match) -> str:
        return _P if parser.normalize(m.group(1)) in names else m.group(0)

    parts = re.split(r"(\[\[.*?\]\])", text)            # only touch plain text between existing links
    out = []
    for i, part in enumerate(parts):
        out.append(parser.WIKILINK_RE.sub(link, part) if i % 2 else rx.sub(_P, part))
    t = "".join(out)
    t = re.sub(rf"{_P}(?:['’]s)", "someone's", t)
    t = re.sub(rf"{_P}\s*(?:,|&|and)\s+", "", t)         # 'Rahul, Priya' / 'Rahul and Priya'
    t = re.sub(rf"\s*(?:,|&|and)\s+{_P}", "", t)         # 'Priya, Rahul' / 'Priya and Rahul'
    t = t.replace(_P, "someone")
    t = re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", t)).strip()
    meaningful = re.sub(r"\b(someone|someone's)\b", "", t, flags=re.I)
    if len(re.findall(r"\w+", meaningful)) < 2:
        return None
    return t


# ---- planning -----------------------------------------------------------------------------------------------------
def build_plan(index: Index, query: str) -> Plan:
    """What forgetting `query` would do. The index must be in sync."""
    plan = Plan()
    ents = [e for e in index.find_entities(re.sub(r"['’]s\b", "", query), fuzzy=False, stopwords=STOPWORDS)
            if e[2] >= 1.0][:1]
    left = [w for w in re.findall(r"[\w']+", query.casefold()) if w not in FILLER]
    title_of = lambda nid: index.note(nid)["title"]  # noqa: E731

    if not ents:                                         # topic query: delete the facts that match all its words
        for r in index.search_blocks(terms(query), limit=10, match_all=True):
            plan.facts.append(Item(r["note_id"], r["block_id"], title_of(r["note_id"]), r["text"]))
        return plan

    nid, phrase, _ = ents[0]
    row = index.note(nid)
    ent_words = set(phrase.split()) | {w + "s" for w in phrase.split()}
    rest = [w.removesuffix("'s") for w in left]
    rest = [w for w in rest if w not in ent_words]
    if row["type"] in SINGLETONS or rest:                # a specific fact about the entity: delete just those
        want = terms(" ".join(rest)) or None
        for b in index.blocks_of([nid]):
            if want is None or any(t in parser.strip_links(b["text"]).casefold() for t in want):
                plan.facts.append(Item(nid, b["block_id"], row["title"], b["text"]))
        return plan

    # "forget everything about Rahul"
    plan.notes.append((nid, row["title"], row["path"]))
    plan.note_facts = len(index.blocks_of([nid]))
    names = index.names_of(nid)
    rx = name_regex(names)
    found: dict[tuple[str, str], dict] = {}
    for r in index.blocks_linking_to([nid]):             # facts elsewhere that link to the person
        found[(r["note_id"], r["block_id"])] = r
    name_terms = terms(re.sub(r"[-_]", " ", row["title"]))
    for r in index.search_blocks(name_terms, limit=100, match_all=True) if name_terms else []:
        if mentions_name(r["text"], rx):                 # plain-text mentions with no link
            found.setdefault((r["note_id"], r["block_id"]), r)
    for (n, b), r in sorted(found.items()):
        if n == nid:
            continue
        item = Item(n, b, title_of(n), r["text"])
        if is_subject(r["text"], rx):
            plan.subject.append(item)
        else:
            item.new_text = redact(r["text"], names)
            plan.mentions.append(item)
    return plan


def preview(plan: Plan) -> str:
    """Human-readable summary of a plan, grouped a / b / c."""
    show = lambda i: f"  [{i.title}] {parser.strip_links(i.text)}"  # noqa: E731
    out: list[str] = []
    for _nid, title, _path in plan.notes:
        out.append(f"Recycle Bin: the note \"{title}\" ({plan.note_facts} fact(s) inside)")
    if plan.subject:
        out += ["Delete (facts mainly about them):", *(show(i) for i in plan.subject)]
    if plan.mentions:
        out.append("Mentions (default: remove just their name; you can choose delete per fact):")
        for i in plan.mentions:
            after = parser.strip_links(i.new_text) if i.new_text else "(nothing left, will be deleted)"
            out.append(f"{show(i)}\n      -> {after}")
    if plan.facts:
        out += ["Delete:", *(show(i) for i in plan.facts)]
    return "\n".join(out)


# ---- execution ----------------------------------------------------------------------------------------------------
def execute(index: Index, vault: Vault, plan: Plan, default: MentionAction = "redact",
            choices: dict[tuple[str, str], MentionAction] | None = None) -> dict:
    """Carry out a plan. Returns counts only: {'notes': [titles], 'deleted': n, 'redacted': n, 'errors': [...]}."""
    choices = choices or {}
    doomed = {nid for nid, _t, _p in plan.notes}
    delete: dict[str, set[str]] = {}
    redact_to: dict[str, dict[str, str]] = {}
    for item in [*plan.subject, *plan.facts]:
        delete.setdefault(item.note_id, set()).add(item.block_id)
    for item in plan.mentions:
        if choices.get(item.key, default) == "redact" and item.new_text:
            redact_to.setdefault(item.note_id, {})[item.block_id] = item.new_text
        else:
            delete.setdefault(item.note_id, set()).add(item.block_id)

    deleted = redacted = 0
    errors: list[str] = []
    for nid in {*delete, *redact_to} - doomed:
        path = index.path_of(nid)
        if not path or not path.exists():
            continue

        local = [0, 0]

        def apply(note, nid=nid, local=local):                # may run twice if the file changes underneath us
            local[0] = Vault.delete_facts(note, delete.get(nid, set()))
            local[1] = sum(Vault.replace_fact_text(note, bid, text) for bid, text in redact_to.get(nid, {}).items())

        try:
            vault.mutate(path, apply)
            deleted += local[0]
            redacted += local[1]
        except OSError as e:
            errors.append(f"could not edit {path.name}: {e}")
    trashed: list[str] = []
    for nid, title, rel in plan.notes:
        path = vault.root / rel
        try:
            send2trash(str(path))                        # Recycle Bin only; if that fails the file stays
            trashed.append(title)
        except Exception as e:  # noqa: BLE001
            errors.append(f"could not move '{title}' to the Recycle Bin: {e}")
    index.sync()
    log.info("forget: notes=%s deleted_facts=%d redacted_facts=%d errors=%d", trashed, deleted, redacted, len(errors))
    return {"notes": trashed, "deleted": deleted, "redacted": redacted, "errors": errors}
