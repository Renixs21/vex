"""Regex filter for things that must never be stored in memory. Runs before any write, independent of the LLM."""
from __future__ import annotations

import re

_PATTERNS: dict[str, re.Pattern] = {
    "api key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|gsk_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{30,})\b"),
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.I),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password": re.compile(r"\b(?:pass(?:word|code|phrase)|passwd|pwd)\b\s*(?:is|was|=|:)\s*\S+", re.I),
    "pin/otp": re.compile(r"\b(?:pin|otp|cvv|cvc|one[- ]time (?:code|password))\b\s*(?:is|was|=|:)?\s*\d{3,8}\b", re.I),
    "secret assignment": re.compile(r"\b(?:api[_ -]?key|secret|token)\b\s*(?:is|=|:)\s*[A-Za-z0-9_\-./+=]{12,}", re.I),
    "aadhaar": re.compile(r"(?<!\d[ -])\b\d{4}[ -]\d{4}[ -]\d{4}\b(?![ -]\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}
_CARD_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){13,19}(?![\d-])")
_LONG_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def find_secrets(text: str) -> list[str]:
    """Names of the secret kinds found in `text` (empty list = clean)."""
    found = [kind for kind, rx in _PATTERNS.items() if rx.search(text)]
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card number")
            break
    if _LONG_TOKEN_RE.search(text):
        found.append("long token")
    return found


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


_ALL_RX = [*_PATTERNS.values(), _LONG_TOKEN_RE]


def redact(text: str, known_values: list[str] | None = None) -> str:
    """Replace anything that looks like a secret with [redacted]. `known_values` are literal secrets (e.g. API keys
    loaded from .env) that are removed wherever they appear."""
    for v in known_values or []:
        if len(v) >= 8:
            text = text.replace(v, "[redacted]")
    for rx in _ALL_RX:
        text = rx.sub("[redacted]", text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[redacted]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return _CARD_RE.sub(card, text)
