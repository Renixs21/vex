"""run_powershell: red tier. The user always sees the exact command; risky and opaque commands are handled in
safety.shell_rules (refused, or a typed CONFIRM); administrator rights are a separate, later confirmation."""
from __future__ import annotations

import subprocess

from safety import elevate, shell_rules
from safety.policy import Decision
from tools.apps import child_env
from tools.registry import RED, Refuse, ToolResult, tool

MAX_OUTPUT = 4000
MAX_TIMEOUT = 120
# Makes PowerShell write UTF-8 so non-ASCII output survives; it changes nothing about what the command does.
_OUTPUT_PREFIX = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "


def _prepare(a: dict) -> dict:
    command = str(a["command"]).strip()
    analysis = shell_rules.analyze(command)
    if analysis.refused:
        raise Refuse(analysis.refused)
    try:
        timeout = max(1, min(int(a.get("timeout", 30)), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = 30
    return {"command": command, "admin": bool(a.get("admin", False)), "timeout": timeout}


def _classify(a: dict) -> Decision:
    analysis = shell_rules.analyze(a["command"])
    warnings = [f"This command {why}" for why in analysis.catastrophic] + [f"This command {w}" for w in analysis.warnings]
    return Decision(RED, "PowerShell can change anything on this PC", second_confirm=bool(analysis.catastrophic),
                    elevate=bool(a.get("admin")), warnings=warnings)


def _describe(a: dict) -> str:
    who = "as ADMINISTRATOR " if a.get("admin") else ""
    return f"Run PowerShell {who}(timeout {a['timeout']}s). Exactly this command:\n----\n{a['command']}\n----"


@tool("run_powershell",
      "Run a PowerShell command; the user approves the exact text. No encoded commands.",
      {"command": {"type": "string", "description": "Plain readable PowerShell"},
       "admin": {"type": "boolean", "description": "Administrator rights; only if the user asked for them", "default": False},
       "timeout": {"type": "integer", "description": "Seconds, max 120", "default": 30}},
      tier=RED, prepare=_prepare, classify=_classify, describe=_describe)
def run_powershell(command: str, admin: bool = False, timeout: int = 30) -> ToolResult:
    if admin:
        ok, out = elevate.run_powershell_elevated(command, timeout)
        return ToolResult(ok, _trim(out) or "(no output)")
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OUTPUT_PREFIX + command],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=child_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"Stopped after {timeout} seconds: the command took too long.")
    out = _trim((p.stdout or "").strip() + (("\n" + p.stderr.strip()) if p.stderr and p.stderr.strip() else ""))
    if p.returncode:
        return ToolResult(False, f"Exit code {p.returncode}. {out}".strip())
    return ToolResult(True, out or "(no output)")


def _trim(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n...[output truncated]"
