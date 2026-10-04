"""Tools about Vex itself.

go_to_sleep: the brain's way to put Vex to sleep for any phrasing the fast path (ui/controller.py done_intent) doesn't catch, like
"I'm going to bed, you rest too" or "okay we're finished here". It only raises a flag; after the reply the controller does exactly
what the fast path does: a short spoken reply, the orb parks as the pill, wake word only, no follow-up window.
"""
from __future__ import annotations

import threading

from tools.registry import GREEN, ToolResult, tool

_requested = threading.Event()


def clear_sleep_request() -> None:
    """Called before each turn's brain run."""
    _requested.clear()


def take_sleep_request() -> bool:
    """True once if go_to_sleep was called during this turn."""
    was = _requested.is_set()
    _requested.clear()
    return was


@tool("go_to_sleep", "Go to sleep when the user is done or asks you to.", {}, tier=GREEN,
      describe=lambda a: "Go to sleep")
def go_to_sleep() -> ToolResult:
    _requested.set()
    return ToolResult(True, "Vex will go to sleep after this reply. Reply with one or two words, like 'Okay.'")
