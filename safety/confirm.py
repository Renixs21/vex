"""The Confirmer: the ONLY way an action gets approved.

Nothing the LLM says and nothing in a tool result can approve anything: the executor asks a Confirmer, and only a
Confirmer's answer (from the user) counts. Approvals are per call and never carry over.

Voice plan: red-tier confirmations and typed confirmations set `requires_non_voice`. A voice-capable Confirmer must
collect those through the hotkey, a tray click or typing, never from speech, so a video saying "yes" can't approve a
dangerous action. Yellow confirmations may be answered by voice. The executor double-checks by rejecting any approval to a
`requires_non_voice` request whose `channel` is "voice".
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

Channel = Literal["typed", "dialog", "hotkey", "tray", "voice", "none"]


@dataclass
class ConfirmRequest:
    tool: str
    tier: str                                  # yellow | red
    title: str
    detail: str                                # the exact thing that will run (command, path, package...)
    warnings: list[str] = field(default_factory=list)
    kind: Literal["yes_no", "typed"] = "yes_no"
    typed_word: str | None = None              # for kind="typed": the exact word the user must type
    requires_non_voice: bool = False           # True for red and typed: voice-only answers are not accepted
    timeout: float = 30.0                      # seconds; no answer = no
    step: Literal["action", "catastrophic", "elevation"] = "action"


@dataclass
class Confirmation:
    approved: bool
    channel: Channel = "none"
    reason: str = ""                           # "timeout", "declined", "no_input", ...


class Confirmer(Protocol):
    def confirm(self, req: ConfirmRequest) -> Confirmation: ...


class DenyAllConfirmer:
    """Used when nobody can be asked (no terminal): everything that needs approval is declined."""

    def confirm(self, req: ConfirmRequest) -> Confirmation:
        return Confirmation(False, "none", "no_input")


def _read_line(prompt: str, timeout: float) -> str | None:
    """Read a line from the console with a timeout (None on timeout). Polls msvcrt on Windows."""
    print(prompt, end="", flush=True)
    try:
        import msvcrt
    except ImportError:                          # non-Windows fallback: blocking read, no timeout
        return sys.stdin.readline().rstrip("\r\n")
    buf: list[str] = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if msvcrt.kbhit():
            ch = msvcrt.getwche()
            if ch in ("\r", "\n"):
                print()
                return "".join(buf)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\b":
                if buf:
                    buf.pop()
                    print(" \b", end="", flush=True)
            else:
                buf.append(ch)
        else:
            time.sleep(0.03)
    print("\n(no answer, treating that as no)")
    return None


class TerminalConfirmer:
    """Asks in the console. Typing is a non-voice channel, so it satisfies red and typed requests."""

    def __init__(self, reader=_read_line):
        self._read = reader

    def confirm(self, req: ConfirmRequest) -> Confirmation:
        bar = "=" * 60
        print(f"\n{bar}\n[{req.tier.upper()}] {req.title}\n{req.detail}")
        for w in req.warnings:
            print(f"  ! {w}")
        if req.kind == "typed":
            prompt = f"Type {req.typed_word} to go ahead, anything else cancels: "
        else:
            prompt = "Approve? [y/N]: "
        answer = self._read(prompt, req.timeout)
        if answer is None:
            return Confirmation(False, "none", "timeout")
        answer = answer.strip()
        if req.kind == "typed":
            ok = answer == req.typed_word
        else:
            ok = answer.lower() in ("y", "yes")
        return Confirmation(ok, "typed", "" if ok else "declined")
