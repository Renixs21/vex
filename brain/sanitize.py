"""Final output filter: what the model says is cleaned before it reaches captions or text-to-speech.

Real replies from the Groq fallback model contained: zero-width characters and runs of "..." (which a speech engine
reads out), sentences glued together ("Muted.Done."), markdown, drafts followed by the final answer, and the model's own
notes to itself ("Need to give concise reply confirming brightness set."). This module removes all of that.

Meta-text is only removed when a sentence *starts* like an internal note (e.g. "Need to give ...", "The user wants ...",
"The response got garbled"). Ordinary replies such as "I need to know which file you mean." or "The answer is four." are kept.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_REMOVE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))            # zero-width and soft hyphen
_TO_SPACE = dict.fromkeys(map(ord, "        "), " ")
_TO_HYPHEN = dict.fromkeys(map(ord, "‐‑"), "-")

_META = [re.compile(p, re.I) for p in (
    r"^(we|i)\s+(need|have|must|should|will|can|could|want)\s+to\s+(give|provide|respond|reply|answer|output|produce|craft|compose|"
    r"keep\s+it|be\s+(concise|brief)|mention|tell\s+the\s+user|say\s+that)\b",
    r"^need\s+to\s+(give|provide|respond|reply|answer|output|produce|craft|compose|keep|be\s+(concise|brief)|mention|say)\b",
    r"^(the\s+)?(user|assistant)\s+(wants|asked|asks|said|says|is\s+asking|requested|needs)\b",
    r"^the\s+(response|output|reply)\s+(got|is|was)\s+garbled\b",
    r"^the\s+(response|output|reply)\s+should\b",
    r"^it\s+looks\s+like\s+the\s+(answer|response|output|reply)\b",
    r"^let'?s\s+(craft|produce|respond|answer|compose|draft)\b",
    r"^let\s+me\s+(think|craft|respond|reply|answer|draft|formulate)\b",
    r"^(final\s+answer|analysis|commentary)\s*[:\-]",
    r"^(okay|ok|alright)[,.]?\s+(so\s+)?(the\s+user|we\s+need|i\s+need\s+to\s+(give|respond|answer|reply))\b",
    r"^(hmm|wait)[,.…]",
    r"^(i'm|i\s+am)\s+(going\s+to|about\s+to)\s+(craft|produce|write|respond|give|answer|compose)\b",
)]
_ELLIPSIS_TAIL = re.compile(r"(?:\s*(?:…|\.{3,}))+\s*$")
_SENTENCE_GLUE = re.compile(r"(?<=[a-z0-9)\"'”][.!?])(?=[A-Z])|(?<=[A-Z]{2}[.!?])(?=[A-Z][a-z])")      # also "USA.He"
_TOKEN = re.compile(r"[\w\-']+", re.U)


def _is_meta(sentence: str) -> bool:
    s = re.sub(r"^[\W_]+", "", sentence)
    return any(rx.search(s) for rx in _META)


_FUNCTION_WORDS = frozenset("""the a an is are was were be been to of in on for and or it its your you i my me with as at by that this
has have had will can do does did not no so set done""".split())


def _tokens(sentence: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(sentence)]


def _content(sentence: str) -> set[str]:
    return {t for t in _tokens(sentence) if t not in _FUNCTION_WORDS and not re.fullmatch(r"[-']+", t)}


def _same_point(a: str, b: str) -> bool:
    """True if sentence `a` is a draft of the next sentence `b`: they share at least two *content* words (names, numbers,
    nouns: not 'is' or 'the'), and `a` starts the same way as `b` or is (nearly) contained in it. Two facts that merely
    share a subject ("Rahul is your roommate. Rahul is also helping...") share one word and are both kept."""
    ca, cb = _content(a), _content(b)
    shared = ca & cb
    if len(shared) < 2:
        return False
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb and ta[0] == tb[0]) or len(shared) / len(ca) >= 0.7 or fuzz.token_set_ratio(a, b) >= 90


def clean_reply(text: str) -> str:
    """The reply with invisible characters, markdown, filler, internal notes and duplicate drafts removed."""
    if not text:
        return ""
    t = text.translate(_REMOVE).translate(_TO_SPACE).translate(_TO_HYPHEN)
    t = re.sub(r"\*\*|__|`", "", t)                                                  # emphasis / code markers
    lines: list[str] = []
    for line in t.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s+|[-*•]\s+)", "", line)                  # headings and bullets
        line = _ELLIPSIS_TAIL.sub("", line.strip())
        line = re.sub(r"\s{2,}", " ", line).strip()
        if re.search(r"[^\W_]", line):                                               # drop lines with no words at all
            lines.append(line)
    sentences: list[str] = []
    for line in lines:
        line = _SENTENCE_GLUE.sub(" ", line)                                         # "Muted.Done." -> "Muted. Done."
        sentences += [s for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]

    meta_idx = [i for i, s in enumerate(sentences) if _is_meta(s)]
    if meta_idx:
        last = meta_idx[-1]
        if last + 1 < len(sentences):                                                # the answer follows the notes
            sentences = sentences[last + 1:]
        else:                                                                        # notes came last: keep what precedes them
            sentences = [s for i, s in enumerate(sentences) if i not in meta_idx]

    kept: list[str] = []
    for i, s in enumerate(sentences):                                                # drop a draft followed by its final form
        if i + 1 < len(sentences) and _same_point(s, sentences[i + 1]):
            continue
        kept.append(s)
    return re.sub(r"\s{2,}", " ", " ".join(kept)).strip()
