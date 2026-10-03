"""Markdown parsing for VexMind notes: wikilinks, tags, fact blocks (`^f1`) and provenance comments."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

WIKILINK_RE = re.compile(r"\[\[([^\[\]|#^]+?)(?:#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")
TAG_RE = re.compile(r"(?<![\w/&#])#([A-Za-z][\w/-]*)")
BULLET_RE = re.compile(r"^(?P<indent>\s*)(?P<mark>[-*+])\s+(?P<body>.+?)\s*$")
CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s")
BLOCK_ID_RE = re.compile(r"\s+\^(?P<id>[A-Za-z0-9-]+)$")
PROVENANCE_RE = re.compile(
    r"^\s*<!--\s*src:\s*(?P<src>.*?)\s*·\s*learned:\s*(?P<learned>\S+?)(?:\s*·\s*(?P<history>.*?))?\s*-->\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


@dataclass
class Block:
    """One fact bullet."""
    block_id: str
    text: str                       # bullet text without the ^id marker
    source: str = ""
    learned: str = ""
    history: str = ""              # e.g. 'was: 20th, updated 2026-10-03'
    line: int = 0                   # 0-based line index of the bullet in the body
    links: list[str] = field(default_factory=list)   # raw wikilink targets in this fact


@dataclass
class Parsed:
    blocks: list[Block]
    links: list[tuple[str, str | None]]   # (raw target, block_id or None)
    tags: set[str]


def normalize(name: str) -> str:
    """Canonical form for name matching: lowercase, '-' and '_' as spaces, collapsed whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", name.casefold())).strip()


def slugify(title: str) -> str:
    """Filename stem for a note title: 'Portfolio Site' -> 'portfolio-site'."""
    s = re.sub(r"[^\w\s-]", "", title.casefold(), flags=re.U)
    s = re.sub(r"[\s_]+", "-", s.strip())
    return re.sub(r"-{2,}", "-", s).strip("-") or "note"


def wikilinks(text: str) -> list[str]:
    """Raw link targets (the part before any `|alias` or `#heading`)."""
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(text)]


def strip_links(text: str) -> str:
    """`[[rahul|Rahul]]` -> `Rahul`, `[[portfolio-site]]` -> `portfolio-site`."""
    return WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)


def inline_tags(text: str) -> set[str]:
    """#tags in prose; ignores headings, wikilinks and HTML comments."""
    clean = COMMENT_RE.sub(" ", WIKILINK_RE.sub(" ", text))
    return {m.group(1).casefold() for m in TAG_RE.finditer(clean)}


def split_block_id(bullet_body: str) -> tuple[str, str | None]:
    """Return (text, block_id|None) for a bullet's body."""
    m = BLOCK_ID_RE.search(bullet_body)
    if not m:
        return bullet_body.strip(), None
    return bullet_body[: m.start()].rstrip(), m.group("id")


def iter_bullets(body: str):
    """Yield (line_index, indent, mark, body) for fact bullets, skipping code fences and checkbox tasks."""
    in_fence = False
    for i, line in enumerate(body.splitlines()):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = BULLET_RE.match(line)
        if m and not CHECKBOX_RE.match(m.group("body")):
            yield i, m.group("indent"), m.group("mark"), m.group("body")


def parse_body(body: str) -> Parsed:
    """Extract fact blocks, links and tags from a note body (frontmatter already removed)."""
    lines = body.splitlines()
    blocks: list[Block] = []
    for i, _indent, _mark, raw in iter_bullets(body):
        text, bid = split_block_id(raw)
        if bid is None:
            continue
        b = Block(block_id=bid, text=text, line=i, links=wikilinks(text))
        if i + 1 < len(lines) and (pm := PROVENANCE_RE.match(lines[i + 1])):
            b.source, b.learned, b.history = pm.group("src"), pm.group("learned"), pm.group("history") or ""
        blocks.append(b)

    by_line = {b.line: b.block_id for b in blocks}
    links: list[tuple[str, str | None]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in wikilinks(COMMENT_RE.sub("", line)):
            links.append((target, by_line.get(i)))
    return Parsed(blocks=blocks, links=links, tags=inline_tags("\n".join(_outside_fences(lines))))


def _outside_fences(lines: list[str]):
    in_fence = False
    for line in lines:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def format_fact(text: str, block_id: str, source: str, learned: str) -> str:
    """The two-line markdown for a fact: bullet with ^id, then the provenance comment."""
    return f"- {text}  ^{block_id}\n  <!-- src: {source} · learned: {learned} -->\n"


def next_block_id(body: str) -> str:
    """Next free `fN` id in a body."""
    used = {int(m.group(1)) for m in re.finditer(r"\^f(\d+)\b", body)}
    return f"f{max(used, default=0) + 1}"


def assign_missing_ids(body: str) -> tuple[str, bool]:
    """Give hand-written bullets stable `^fN` ids. Returns (new_body, changed)."""
    lines = body.split("\n")
    n = max((int(m.group(1)) for m in re.finditer(r"\^f(\d+)\b", body)), default=0)
    changed = False
    for i, _indent, _mark, raw in iter_bullets(body):
        _text, bid = split_block_id(raw)
        if bid is None:
            n += 1
            lines[i] = lines[i].rstrip() + f"  ^f{n}"
            changed = True
    return "\n".join(lines), changed


def rewrite_links(text: str, old_names: set[str], new_stem: str) -> str:
    """Point every wikilink whose target normalizes into `old_names` at `new_stem`, keeping |display text."""

    def repl(m: re.Match) -> str:
        if normalize(m.group(1)) not in old_names:
            return m.group(0)
        whole = m.group(0)
        heading = re.search(r"#[^\[\]|]*", whole[2:])
        disp = m.group(2)
        return "[[" + new_stem + (heading.group(0) if heading else "") + (f"|{disp}" if disp is not None else "") + "]]"

    return WIKILINK_RE.sub(repl, text)
