"""Tool registry: @tool registers a function with a JSON schema, a base risk tier and optional safety hooks.

Hooks (all optional, all pure/read-only until the user has approved):
  prepare(args)  -> args   resolve things for display (absolute paths, winget package IDs...). May raise Refuse or
                           ShortCircuit.
  classify(args) -> Decision   raise the tier based on the arguments (e.g. a protected path).
  describe(args) -> str    the exact text the user is asked to approve (its first line is also what is read aloud).
  on_decline(args) -> args|None   if the user explicitly says no, an alternative call to run instead (it goes through the
                           whole safety pipeline again), e.g. "replace the file?" no -> save under a new name.
The registry never executes anything on its own authority: all calls from the agent go through
safety.executor.Executor, which owns tiers, confirmations, dry-run, audit and undo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("vex.tools")

GREEN, YELLOW, RED = "green", "yellow", "red"
TIER_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# Argument names that look like approvals. A tool must never accept these: approval comes only from the user,
# through the Confirmer, never from anything the LLM says or passes.
RESERVED_ARGS = frozenset({"confirm", "confirmed", "approve", "approved", "user_approved", "yes", "force", "skip_confirmation",
                           "no_confirm", "bypass", "i_confirm", "override", "auto_approve", "dry_run"})


class Refuse(Exception):
    """Raised by prepare(): the call is refused outright (message goes back to the LLM)."""


class ShortCircuit(Exception):
    """Raised by prepare(): nothing to do or confirm; return this result as is."""

    def __init__(self, result: "ToolResult"):
        super().__init__(result.message)
        self.result = result


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: Any = None
    undo: dict | None = None        # how to reverse this action (kept by the executor, never shown to the LLM)
    audit: str | None = None        # text for the audit log when the tool's args/results must not be logged

    def as_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    tier: str
    fn: Callable[..., ToolResult] = field(repr=False)
    prepare: Callable[[dict], dict] | None = field(default=None, repr=False)
    classify: Callable[[dict], Any] | None = field(default=None, repr=False)
    describe: Callable[[dict], str] | None = field(default=None, repr=False)
    audit_args: bool = True         # False: the audit log records no arguments (memory, clipboard)
    on_decline: Callable[[dict], dict | None] | None = field(default=None, repr=False)   # args for a gentler alternative if the user says no
    category: str = "core"          # which tool group offers it to the LLM (see tools/categories.py)


_TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict[str, dict], required: list[str] | None = None, tier: str = GREEN, *,
         prepare=None, classify=None, describe=None, audit_args: bool = True, on_decline=None):
    """Register `fn(**args) -> ToolResult`. `params` maps arg name -> JSON-schema property."""
    bad = RESERVED_ARGS & set(params)
    if bad:
        raise ValueError(f"tool {name} declares approval-style argument(s) {sorted(bad)}; approvals come only from the user")
    if required is None:
        required = [k for k, v in params.items() if "default" not in v]
    schema = {"type": "object", "properties": params, "required": required}

    def deco(fn: Callable[..., ToolResult]):
        _TOOLS[name] = Tool(name, description, schema, tier, fn, prepare, classify, describe, audit_args, on_decline=on_decline)
        return fn

    return deco


def all_tools() -> list[Tool]:
    return list(_TOOLS.values())


def get(name: str) -> Tool | None:
    return _TOOLS.get(name)


def tools_for(categories: set[str] | frozenset[str] = frozenset()) -> list[Tool]:
    """The tools to offer the LLM: every core tool plus those in the given categories."""
    return [t for t in _TOOLS.values() if (t.category == "core" or t.category in categories) and t.name not in _disabled]


def execute(name: str, args: dict | None) -> ToolResult:
    """Run a tool function directly (no safety checks: used by the executor after approval, and by unit tests).
    Never raises: errors come back as a ToolResult the LLM can read."""
    t = _TOOLS.get(name)
    if t is None:
        return ToolResult(False, f"Unknown tool '{name}'.")
    try:
        res = t.fn(**(args or {}))
        return res if isinstance(res, ToolResult) else ToolResult(True, str(res))
    except TypeError as e:
        return ToolResult(False, f"Bad arguments for {name}: {e}")
    except Exception as e:  # noqa: BLE001 - tool errors must not escape into the agent loop
        log.exception("tool %s failed", name)
        return ToolResult(False, f"{name} failed: {e}")


# ---- switching tools off ------------------------------------------------------------------------------------------------------------------
# A switched-off tool is never offered to the model and the executor refuses to run it. The set is saved in data/tools_disabled.json.
# Nothing switches a tool back on by itself; a red tool can only be switched back on after an on-screen confirmation (ui/api_core.py).
_disabled: set[str] = set()
_disabled_path = None


def _default_disabled_path():
    from config import ROOT

    return ROOT / "data" / "tools_disabled.json"


def load_disabled(path=None) -> set[str]:
    """Read the switched-off set (at startup). Unknown or unreadable file: nothing switched off."""
    import json

    global _disabled_path
    _disabled_path = path or _default_disabled_path()
    try:
        names = json.loads(_disabled_path.read_text(encoding="utf-8")).get("disabled", [])
        _disabled.clear()
        _disabled.update(str(n) for n in names)
    except (OSError, ValueError, AttributeError):
        _disabled.clear()
    return set(_disabled)


def is_enabled(name: str) -> bool:
    return name not in _disabled


def set_enabled(name: str, enabled: bool) -> None:
    """Switch a tool on/off and save the choice. The caller decides whether that is allowed (red tools need an on-screen yes)."""
    import json

    (_disabled.discard if enabled else _disabled.add)(name)
    path = _disabled_path or _default_disabled_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"disabled": sorted(_disabled)}), encoding="utf-8")
    except OSError:
        log.warning("could not save the switched-off tools", exc_info=True)


def disabled() -> set[str]:
    return set(_disabled)
