"""Shared test doubles."""
from __future__ import annotations

from brain.llm import LLMResponse, ToolCall
from safety.confirm import Confirmation, ConfirmRequest


class ScriptedConfirmer:
    """Answers confirmation requests from a script and records every request. Out of answers = decline."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[ConfirmRequest] = []

    def confirm(self, req: ConfirmRequest) -> Confirmation:
        self.requests.append(req)
        if not self.answers:
            return Confirmation(False, "none", "no_input")
        a = self.answers.pop(0)
        if isinstance(a, bool):
            return Confirmation(a, "typed", "" if a else "declined")
        return a


YES = Confirmation(True, "typed")
NO = Confirmation(False, "typed", "declined")
TIMEOUT = Confirmation(False, "none", "timeout")
VOICE_YES = Confirmation(True, "voice")


class FakeLLM:
    """Plays back scripted responses: each item is a text reply or a list of (tool name, args) calls."""

    def __init__(self, *script):
        self.script = list(script)
        self.systems: list[str] = []
        self.histories: list[list[dict]] = []
        self.tools_seen: list[list[str]] = []          # names of the tools offered at each request
        self.prefer = None                             # the agent's routing hint (provider to try first)
        self.prefer_seen: list = []

    def generate(self, system, messages, tools):
        self.systems.append(system)
        self.tools_seen.append([t.name for t in tools])
        self.prefer_seen.append(self.prefer)
        self.histories.append([dict(m) for m in messages])
        step = self.script.pop(0)
        if isinstance(step, str):
            return LLMResponse(step, [])
        return LLMResponse("", [ToolCall(n, a) for n, a in step])
