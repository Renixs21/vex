"""Hybrid recall: entity match + full-text search + 1-hop graph expansion -> a small context pack for the LLM."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

from memory import parser
from memory.embed import Embedder
from memory.index import Index
from memory.vault import now_iso

STOPWORDS = frozenset("""a about above after again all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here hers him his
how i if in into is it its just me more most my no nor not now of off on once only or other our out over own same she should so
some such than that the their them then there these they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours tell know remember please hey vex whats who's what's""".split())

W_TEXT, W_ENTITY, W_LINKED, W_NEIGHBOR = 1.0, 1.0, 0.5, 0.35
MAX_PER_NOTE = 8
ALWAYS_ON_CHARS = 600


@dataclass
class Item:
    note_id: str
    title: str
    block_id: str
    text: str
    score: float
    reasons: set[str] = field(default_factory=set)

    def line(self) -> str:
        return f"[{self.title}] {parser.strip_links(self.text)}"


@dataclass
class ContextPack:
    items: list[Item]
    always_on: list[str]
    text: str
    tokens: int

    def __bool__(self) -> bool:
        return bool(self.text)


def terms(query: str) -> list[str]:
    """Searchable words in a query: alphanumeric, not stopwords, de-duplicated, in order."""
    seen: dict[str, None] = {}
    for w in re.findall(r"[\w']+", query.casefold()):
        w = w.strip("'")
        if len(w) >= 2 and w not in STOPWORDS:
            seen.setdefault(w)
    return list(seen)


def _approx_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _recency(learned: str) -> float:
    try:
        age = (date.today() - date.fromisoformat(learned[:10])).days
    except ValueError:
        return 0.0
    return 0.2 * math.exp(-max(age, 0) / 90)


def retrieve(index: Index, query: str, budget_tokens: int = 1500, max_items: int = 20, touch: bool = True,
             embedder: Embedder | None = None) -> ContextPack:
    """Facts relevant to `query`, best first, within a token budget. `embedder` is reserved for later."""
    cand: dict[tuple[str, str], Item] = {}

    def add(row, weight: float, reason: str) -> None:
        key = (row["note_id"], row["block_id"])
        it = cand.get(key)
        if it is None:
            it = cand[key] = Item(row["note_id"], "", row["block_id"], row["text"], _recency(row["learned"] or ""))
        it.score += weight
        it.reasons.add(reason)

    # 1. entity match (names, aliases, fuzzy)
    entities = index.find_entities(query, stopwords=STOPWORDS)[:5]
    ent_score = {nid: sc for nid, _ph, sc in entities}
    for row in index.blocks_of(list(ent_score)):
        add(row, W_ENTITY * ent_score[row["note_id"]], "entity")

    # 2. full text (BM25)
    hits = index.search_blocks(terms(query), limit=30)
    if hits:
        best = min(r["score"] for r in hits)
        for r in hits:
            rel = (r["score"] / best) if best < 0 else 1.0
            add(r, W_TEXT * max(0.05, min(1.0, rel)), "text")

    # 3. one-hop graph expansion around the strongest notes
    seeds = list(ent_score)
    for r in hits[:8]:
        if r["note_id"] not in seeds:
            seeds.append(r["note_id"])
        if len(seeds) >= 6:
            break
    neighbor_ids: list[str] = []
    for nid in seeds:
        out, back = index.neighbors(nid)
        for n in [*out, *back]:
            if n not in seeds and n not in neighbor_ids:
                neighbor_ids.append(n)
    for row in index.blocks_linking_to(seeds):          # facts elsewhere that point at the matched notes
        add(row, W_LINKED, "linked")
    per_neighbor: dict[str, int] = {}
    for row in index.blocks_of(neighbor_ids[:12]):      # a few facts from each neighbouring note
        if per_neighbor.get(row["note_id"], 0) < 3:
            per_neighbor[row["note_id"]] = per_neighbor.get(row["note_id"], 0) + 1
            add(row, W_NEIGHBOR, "neighbor")

    # 4. score with importance + usage, cap per note, fit to budget
    info = index.note_info(list({k[0] for k in cand}))
    for it in cand.values():
        n = info.get(it.note_id)
        if n is None:
            continue
        it.title = n["title"]
        it.score += 0.06 * (n["importance"] - 1) + 0.05 * min(n["use_count"], 10) / 10
    ranked = sorted(cand.values(), key=lambda i: -i.score)
    always, always_ids = _always_on(index)
    used = _approx_tokens("\n".join(always))
    items: list[Item] = []
    per_note: dict[str, int] = {}
    for it in ranked:
        if it.note_id in always_ids and it.note_id not in ent_score:
            continue                                     # already shown as always-on context
        if per_note.get(it.note_id, 0) >= MAX_PER_NOTE or len(items) >= max_items:
            continue
        cost = _approx_tokens(it.line())
        if used + cost > budget_tokens:
            break
        used += cost
        per_note[it.note_id] = per_note.get(it.note_id, 0) + 1
        items.append(it)

    lines = [*always, *(i.line() for i in items)]
    if touch and items:
        index.touch(sorted({i.note_id for i in items}), now_iso())
    return ContextPack(items=items, always_on=always, text="\n".join(lines), tokens=used)


def _always_on(index: Index) -> tuple[list[str], set[str]]:
    """Short profile + preferences facts included with every query."""
    rows = index.list_notes("profile") + index.list_notes("preference")
    ids = [r["id"] for r in rows]
    titles = {r["id"]: r["title"] for r in rows}
    out, size = [], 0
    for b in index.blocks_of(ids):
        line = f"[{titles[b['note_id']]}] {parser.strip_links(b['text'])}"
        if size + len(line) > ALWAYS_ON_CHARS:
            break
        out.append(line)
        size += len(line)
    return out, set(ids)
