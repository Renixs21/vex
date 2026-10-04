"""The executor: the one gate every tool call passes through.

prepare -> classify -> (blocked?) -> confirm -> dry-run? -> run -> sanitize -> undo record -> audit.

Rules enforced here, not in the tools:
  * Approval comes only from the Confirmer. Arguments the LLM supplies can't skip it (unknown arguments are dropped,
    tools can't declare approval-style arguments), and nothing in a tool result is ever read as an answer.
  * Each call is confirmed on its own; approvals are never remembered.
  * Red and typed confirmations are rejected if the Confirmer reports they came by voice.
  * Results are scrubbed of secrets and capped in size before the LLM sees them.
"""
from __future__ import annotations

import json
import logging
import sys
import time

from config import ROOT, settings
from memory.secrets import redact
from safety import context, policy
from safety.audit import AuditLog, _known_secret_values, scrub
from safety.confirm import Confirmation, Confirmer, DenyAllConfirmer, TerminalConfirmer
from safety.elevation import DOWNGRADE_NOTE, HINT, NEEDS_ELEVATION, ElevationPolicy
from safety.undo import UndoStack, configure as configure_undo
from tools import registry
from tools.registry import GREEN, Refuse, ShortCircuit, ToolResult

log = logging.getLogger("vex.executor")

MAX_MESSAGE = 8000
MAX_DATA_JSON = 12000
TRUSTED_CHANNELS = {"typed", "dialog", "hotkey", "tray", "voice"}
ADMIN_TOOLS = {"run_powershell"}               # tools with an `admin` argument that safety/elevation.py gates


class Executor:
    def __init__(self, confirmer: Confirmer, audit: AuditLog, undo: UndoStack | None = None, dry_run: bool | None = None):
        self.confirmer = confirmer
        self.audit = audit
        self.undo = undo
        self._dry_run = dry_run
        self.elevation = ElevationPolicy()        # when may a command run as administrator (see safety/elevation.py)

    @property
    def dry_run(self) -> bool:
        return policy.dry_run_enabled() if self._dry_run is None else self._dry_run

    def set_dry_run(self, value: bool | None) -> None:
        """Turn dry-run on/off for this executor (None = follow the DRY_RUN environment variable)."""
        self._dry_run = value

    # ------------------------------------------------------------------------------------------------------------
    def run(self, name: str, args: dict | None) -> ToolResult:
        """Run one tool call through the full safety pipeline. Never raises."""
        t0 = time.monotonic()
        tool = registry.get(name)
        if tool is None:
            self.audit.record(tool=name, tier="?", decision="unknown_tool", ok=False)
            return ToolResult(False, f"Unknown tool '{name}'.")

        schema = tool.parameters
        args = {k: v for k, v in (args or {}).items() if k in schema["properties"]}     # drop anything undeclared
        missing = [r for r in schema.get("required", []) if r not in args]
        if missing:
            return self._finish(tool, "?", "refused", ToolResult(False, f"Missing arguments: {', '.join(missing)}."), args)

        try:
            if tool.prepare is not None:
                args = tool.prepare(args)
        except ShortCircuit as sc:
            return self._finish(tool, GREEN, "auto", sc.result, args)
        except Refuse as r:
            return self._finish(tool, "?", "refused", ToolResult(False, str(r)), args)
        except Exception as e:  # noqa: BLE001
            log.exception("prepare failed for %s", name)
            return self._finish(tool, "?", "error", ToolResult(False, f"{name} failed: {e}"), args)

        downgraded = False
        if name in ADMIN_TOOLS and args.get("admin"):                    # admin only if the user's own words allow it
            allowed, _why = self.elevation.allows(str(args.get("command", "")))
            if not allowed:
                args = {**args, "admin": False}
                downgraded = True
                log.info("%s: admin=true was not backed by the user; running without administrator rights", name)

        decision = policy.classify(tool, args)
        if decision.blocked:
            return self._finish(tool, decision.tier, "blocked", ToolResult(False, f"I can't do that: {decision.reason}."),
                                args, reason=decision.reason)

        requests = policy.confirm_requests(tool, args, decision)
        if self.dry_run:
            exact = tool.describe(args) if tool.describe else f"{name} {args}"
            res = ToolResult(True, f"[dry run] would run: {exact}")
            return self._finish(tool, decision.tier, "dry_run", res, args,
                                extra={"would_confirm": [r.step for r in requests]})

        outcome = "auto"
        via: dict[str, str] = {}                                   # which channel answered each confirmation step
        for req in requests:
            answer = self._ask(req)
            via[req.step] = answer.channel
            if not answer.approved:
                outcome = "timeout" if answer.reason == "timeout" else "denied"
                msg = "Not done: the user didn't answer in time." if outcome == "timeout" else "Not done: the user declined."
                alt = tool.on_decline(args) if (outcome == "denied" and tool.on_decline is not None) else None
                if alt:                                            # a gentler alternative (e.g. save under a new name): own pipeline
                    self._finish(tool, decision.tier, outcome, ToolResult(False, msg), args, reason=f"{req.step}: {answer.reason}",
                                 extra={"via": dict(via), "alternative": True})
                    return self.run(name, alt)
                return self._finish(tool, decision.tier, outcome, ToolResult(False, msg), args, reason=f"{req.step}: {answer.reason}",
                                    extra={"via": dict(via)})
            outcome = "approved"

        result = registry.execute(name, args)
        context.mark_result(name, result.ok)                       # reading outside content taints the rest of the turn
        if name in ADMIN_TOOLS:
            result = self._elevation_followup(args, result, downgraded)
        extra: dict = {"via": dict(via)} if via else {}
        if downgraded:
            extra["admin_downgraded"] = True
        return self._finish(tool, decision.tier, outcome, result, args, started=t0, extra=extra or None)

    def _elevation_followup(self, args: dict, result: ToolResult, downgraded: bool) -> ToolResult:
        """After a non-admin run that failed for lack of rights, tell the model to ask the user "Retry as administrator?"."""
        command = str(args.get("command", ""))
        if not args.get("admin") and not result.ok and NEEDS_ELEVATION.search(result.message):
            self.elevation.offer(command)
            return ToolResult(False, f"{result.message}\n{HINT}", {"needs_admin": True})
        if downgraded and not result.ok:
            return ToolResult(False, f"{result.message}\n{DOWNGRADE_NOTE}", result.data)
        return result

    # ------------------------------------------------------------------------------------------------------------
    def _ask(self, req) -> Confirmation:
        try:
            answer = self.confirmer.confirm(req)
        except KeyboardInterrupt:
            return Confirmation(False, "none", "interrupted")
        except Exception:  # noqa: BLE001 - a broken confirmer must fail closed
            log.exception("confirmer failed")
            return Confirmation(False, "none", "error")
        if answer.approved:
            if answer.channel not in TRUSTED_CHANNELS:
                return Confirmation(False, answer.channel, "approval without a recognised channel")
            if (req.requires_non_voice or req.kind == "typed") and answer.channel == "voice":
                log.warning("rejected a voice answer to a %s confirmation for %s", req.tier, req.tool)
                return Confirmation(False, "voice", "voice can't approve this; use the keyboard, hotkey or tray")
        return answer

    def _sanitize(self, result: ToolResult) -> ToolResult:
        known = _known_secret_values()
        message = redact(result.message, known)
        if len(message) > MAX_MESSAGE:
            message = message[:MAX_MESSAGE] + " ...[truncated]"
        data = scrub(result.data)
        if data is not None and len(json.dumps(data, default=str)) > MAX_DATA_JSON:
            data, message = None, message + " (extra data omitted: too large)"
        return ToolResult(result.ok, message, data, undo=result.undo, audit=result.audit)

    def _finish(self, tool, tier: str, decision: str, result: ToolResult, args: dict, reason: str | None = None,
                started: float | None = None, extra: dict | None = None) -> ToolResult:
        clean = self._sanitize(result)
        summary = clean.message if tool.audit_args else (clean.audit or "")
        entry = self.audit.record(
            tool=tool.name, tier=tier, decision=decision, ok=clean.ok, args=args if tool.audit_args else None,
            summary=summary, reason=reason, undo=bool(clean.undo and clean.ok),
            ms=int((time.monotonic() - started) * 1000) if started else None, **(extra or {}))
        if clean.undo and clean.ok and self.undo is not None:
            self.undo.push(entry, clean.undo)
        return ToolResult(clean.ok, clean.message, clean.data)


def build_default_executor(confirmer: Confirmer | None = None, dry_run: bool | None = None) -> Executor:
    """Executor wired to logs/actions.jsonl and logs/undo.json. Asks in the terminal when there is one, else declines."""
    logs = ROOT / settings["paths"]["logs"]
    undo = UndoStack(logs / "undo.json")
    configure_undo(undo)
    if confirmer is None:
        confirmer = TerminalConfirmer() if sys.stdin is not None and sys.stdin.isatty() else DenyAllConfirmer()
    return Executor(confirmer, AuditLog(logs / "actions.jsonl"), undo, dry_run)
