"""What the current conversation turn has seen: the user's own words, and whether untrusted content has been read.

The user's message is the only trusted instruction. Anything that comes back from a tool that reads the outside world (a web page, a file,
the clipboard, search results, window titles, video titles) is untrusted DATA: it can contain text that tries to give Vex orders. Once such
content has entered the turn it is marked here, and tools that act on the PC based on text (typing, pressing keys, opening links) get
stricter: they must be approved on screen, and the dialog warns that the text may not be the user's own.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

# Tools whose successful results are untrusted content.
UNTRUSTED_TOOLS = frozenset({"read_file", "list_dir", "web_search", "web_fetch", "get_clipboard", "play_youtube", "list_windows",
                             "get_weather", "quick_facts"})


@dataclass
class TurnContext:
    user_text: str = ""
    untrusted_from: list[str] = field(default_factory=list)
    launches: list[dict] = field(default_factory=list)       # apps opened this turn: {"name": ..., "before": {window handles}}
    seen_urls: set[str] = field(default_factory=set)         # links that web_search itself returned this turn

    @property
    def tainted(self) -> bool:
        return bool(self.untrusted_from)


_ctx = TurnContext()
_lock = threading.Lock()


def begin_turn(user_text: str) -> None:
    """Called once per message from the USER (the agent does it). Clears the taint."""
    global _ctx
    with _lock:
        _ctx = TurnContext(user_text or "")


def get() -> TurnContext:
    return _ctx


def mark_result(tool: str, ok: bool) -> None:
    """Called by the executor after a tool ran: reading outside content makes the rest of the turn 'tainted'."""
    if ok and tool in UNTRUSTED_TOOLS:
        with _lock:
            if tool not in _ctx.untrusted_from:
                _ctx.untrusted_from.append(tool)


def note_urls(urls: list[str]) -> None:
    """web_search calls this: these exact links came from the search engine, so reading them is expected."""
    with _lock:
        _ctx.seen_urls.update(u for u in urls if u)


def url_was_returned(url: str) -> bool:
    return url in _ctx.seen_urls or url.rstrip("/") in {u.rstrip("/") for u in _ctx.seen_urls}


def note_launch(name: str, windows_before: set[int]) -> None:
    """open_app calls this: which windows existed before the app was started, so a later 'type into it' targets the NEW window."""
    with _lock:
        _ctx.launches.append({"name": (name or "").lower().strip(), "before": set(windows_before)})


def launch_for(query: str) -> dict | None:
    """The app launched this turn that `query` (a window name) refers to, if any."""
    q = (query or "").lower().strip()
    if not q:
        return None
    for l in reversed(_ctx.launches):
        if l["name"] and (q in l["name"] or l["name"] in q):
            return l
    return None


def user_mentions(word: str) -> bool:
    """True if the user's own message contains this word (whole word, any case)."""
    return bool(word) and bool(re.search(r"(?<![\w])" + re.escape(word.lower()) + r"(?![\w])", _ctx.user_text.lower()))
