"""When may a command run as administrator? Only when the user's own words say so. Enforced here, not by the prompt.

`admin=true` on run_powershell is honoured only if one of two triggers holds:
  1. EXPLICIT: the user's current message asks for admin rights ("run it as administrator", "with admin rights", ...).
  2. ACCEPTED OFFER: a command failed without admin with "access denied" / "requires elevation", Vex asked "Retry as
     administrator?", and the user's very next message is a yes. That yes covers only that exact command.
Anything else the model asks for is quietly downgraded to a normal (non-admin) run and noted in the result. Text from tool
results, web pages, files or memory never counts: only the user's message (begin_turn) can switch either trigger on.
An approved admin run still goes through the normal confirmation dialog, the separate elevation dialog and Windows' UAC prompt.
"""
from __future__ import annotations

import re

_ADMIN_WORDS = (
    r"(?:as|with|using|in)\s+(?:an?\s+|the\s+)?(?:admin|administrator|elevated)\b",
    r"\b(?:admin|administrator)\s+(?:rights|privileges?|permissions?|mode|access)\b",
    r"\belevated\s+(?:rights|privileges?|permissions?|mode|powershell|command|prompt)\b",
    r"\brun\s+(?:it\s+)?elevated\b",
)
_ADMIN = re.compile("|".join(_ADMIN_WORDS), re.I)
_NEGATION = re.compile(r"\b(?:not|don'?t|do\s+not|dont|never|without|no|isn'?t|won'?t|stop|avoid)\b[\w\s,']{0,25}$", re.I)

_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|y|please\s+do|do\s+it|go\s+ahead|go\s+for\s+it|retry|try\s+again|"
                  r"retry\s+as\s+admin(?:istrator)?|affirmative)\b", re.I)
_NO = re.compile(r"\b(?:no|not|don'?t|do\s+not|never|cancel|stop|nope|nah)\b", re.I)

# What a failed non-admin PowerShell command says when it needed elevation
NEEDS_ELEVATION = re.compile(
    r"access\s+(?:to\s+the\s+path\s+.{0,200}?\s+)?is\s+denied|access\s+denied|unauthorizedaccess|permission\s+denied|0x80070005|"
    r"requires?\s+elevation|requested\s+operation\s+requires\s+elevation|administrator\s+privileges?\s+(?:are\s+)?required|"
    r"must\s+be\s+(?:run|executed)\s+(?:as|by)\s+(?:an?\s+)?admin|run\s+as\s+administrator|do\s+not\s+have\s+(?:the\s+)?required\s+permission",
    re.I | re.S)

OFFER_QUESTION = "Retry as administrator?"
HINT = (f"[Access was denied or elevation is required. Tell the user so and ask exactly: \"{OFFER_QUESTION}\" "
        "Only run it as administrator if they answer yes.]")
DOWNGRADE_NOTE = ("[Ran without administrator rights because the user did not ask for them. If it fails with access denied, "
                  f"ask: \"{OFFER_QUESTION}\"]")


def asks_for_admin(text: str) -> bool:
    """True if the user's message explicitly asks for administrator rights (and isn't negated: 'not as admin')."""
    for m in _ADMIN.finditer(text or ""):
        if not _NEGATION.search(text[: m.start()][-40:]):
            return True
    return False


def is_yes(text: str) -> bool:
    """A short, plainly affirmative reply ('yes', 'sure, go ahead', 'retry as admin')."""
    t = (text or "").strip()
    return bool(t) and len(t.split()) <= 6 and bool(_YES.match(t)) and not _NO.search(t)


def _norm(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


class ElevationPolicy:
    """Per-conversation state for the two triggers. One instance lives on the Executor."""

    def __init__(self) -> None:
        self.explicit = False
        self._offered: str | None = None            # command we asked "Retry as administrator?" about
        self._accepted: str | None = None           # command the user said yes to, valid for the current turn only

    def begin_turn(self, user_text: str) -> None:
        """Call once per message from the USER. Nothing else may call this."""
        self.explicit = asks_for_admin(user_text)
        self._accepted = None
        if self._offered is not None:
            if is_yes(user_text):
                self._accepted = self._offered
            self._offered = None                     # an offer lives for exactly one reply

    def allows(self, command: str) -> tuple[bool, str]:
        """(allowed, reason) for running `command` as administrator right now."""
        if self.explicit:
            return True, "the user asked for administrator rights"
        if self._accepted is not None and _norm(command) == _norm(self._accepted):
            return True, "the user accepted the administrator retry"
        return False, ""

    def offer(self, command: str) -> None:
        """Remember that we asked 'Retry as administrator?' for this command."""
        self._offered = command

    @property
    def pending_offer(self) -> str | None:
        return self._offered
