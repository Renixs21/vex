"""The VisualFrontend contract: everything the rest of Vex needs from whatever draws Vex on screen.

The temporary Qt pill, a future orb widget, or a separate app (for example Godot) over a local socket all implement
this. Rules, so any implementation (including a remote one) behaves the same:

  * Every method is non-blocking, thread-safe and idempotent; callers may use any thread.
  * set_state(s): s is one of STATES. The frontend should treat a state change as "level is 0 until told otherwise".
  * set_level(x): loudness 0..1 of what Vex is saying right now. Python owns audio playback and pushes this about
    LEVEL_HZ (30) times a second while speaking; frontends smooth it themselves. Values outside 0..1 are clamped.
  * show_caption(text): the text to display (what was heard, or Vex's reply). "" clears it.
  * show() / hide(): make the visual visible or invisible. Hidden frontends still accept all other calls.

Over IPC every call is one JSON object per line (see JsonLinesFrontend and docs/FRONTEND_PROTOCOL.md); calls are
fire-and-forget, so a slow or missing frontend can never stall Vex. Confirmation dialogs are NOT part of this
interface: they stay native Python (ui/confirm_dialog.py) so a frontend can never approve anything.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Protocol

log = logging.getLogger("vex.frontend")

STATES = ("idle", "listening", "thinking", "speaking")
LEVEL_HZ = 30


class VisualFrontend(Protocol):
    def set_state(self, state: str) -> None: ...
    def set_level(self, level: float) -> None: ...
    def show_caption(self, text: str) -> None: ...
    def show(self) -> None: ...
    def hide(self) -> None: ...


def clamp01(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v != v else max(0.0, min(1.0, v))        # v != v is NaN


class NullFrontend:
    """Does nothing. Used in tests and when no visual is wanted."""

    def set_state(self, state: str) -> None: ...
    def set_level(self, level: float) -> None: ...
    def show_caption(self, text: str) -> None: ...
    def show(self) -> None: ...
    def hide(self) -> None: ...


class RecordingFrontend:
    """Remembers every call as (op, value); handy for tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def set_state(self, state: str) -> None:
        self.calls.append(("state", state))

    def set_level(self, level: float) -> None:
        self.calls.append(("level", clamp01(level)))

    def show_caption(self, text: str) -> None:
        self.calls.append(("caption", text))

    def show(self) -> None:
        self.calls.append(("show", None))

    def hide(self) -> None:
        self.calls.append(("hide", None))

    @property
    def states(self) -> list[str]:
        return [v for op, v in self.calls if op == "state"]


# ---- IPC encoding ------------------------------------------------------------------------------------------------------
def encode(op: str, **fields: Any) -> str:
    """One protocol message as a JSON line (no trailing newline)."""
    return json.dumps({"op": op, **fields}, ensure_ascii=False, separators=(",", ":"))


def decode(line: str) -> dict:
    """Parse and validate one protocol line. Raises ValueError for anything malformed."""
    msg = json.loads(line)
    if not isinstance(msg, dict) or "op" not in msg:
        raise ValueError("a message must be a JSON object with an 'op'")
    op = msg["op"]
    if op == "state" and msg.get("state") not in STATES:
        raise ValueError(f"unknown state {msg.get('state')!r}")
    if op == "level" and not isinstance(msg.get("level"), (int, float)):
        raise ValueError("level must be a number")
    if op == "caption" and not isinstance(msg.get("text"), str):
        raise ValueError("caption text must be a string")
    if op not in ("state", "level", "caption", "show", "hide"):
        raise ValueError(f"unknown op {op!r}")
    return msg


def dispatch(frontend: VisualFrontend, msg: dict) -> None:
    """Apply a decoded protocol message to a frontend (the receiving side of the protocol)."""
    op = msg["op"]
    if op == "state":
        frontend.set_state(msg["state"])
    elif op == "level":
        frontend.set_level(msg["level"])
    elif op == "caption":
        frontend.show_caption(msg["text"])
    elif op == "show":
        frontend.show()
    elif op == "hide":
        frontend.hide()


class JsonLinesFrontend:
    """Frontend that sends each call as a JSON line through `write(line)` (a socket, a pipe, a queue...).

    `write` must not block for long; failures are logged and swallowed so a dead frontend never affects Vex.
    Levels are rounded to 3 decimals and repeated identical levels are dropped to keep the stream small.
    """

    def __init__(self, write: Callable[[str], None]):
        self._write = write
        self._last_level: float | None = None

    def _send(self, line: str) -> None:
        try:
            self._write(line + "\n")
        except Exception:  # noqa: BLE001 - never let the display take Vex down
            log.warning("frontend write failed", exc_info=True)

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        self._last_level = None
        self._send(encode("state", state=state))

    def set_level(self, level: float) -> None:
        v = round(clamp01(level), 3)
        if v != self._last_level:
            self._last_level = v
            self._send(encode("level", level=v))

    def show_caption(self, text: str) -> None:
        self._send(encode("caption", text=text))

    def show(self) -> None:
        self._send(encode("show"))

    def hide(self) -> None:
        self._send(encode("hide"))


def sleep_until(deadline: float, stop: threading.Event | None = None) -> bool:
    """Sleep until time.monotonic() reaches `deadline`. Returns True if `stop` was set first.

    Event.wait() on Windows rounds timeouts to the ~15 ms timer tick, which would turn 30 Hz into about 21 Hz;
    short time.sleep() slices against an absolute deadline keep the rate steady and still react to stop quickly.
    """
    while True:
        if stop is not None and stop.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, 0.008))
