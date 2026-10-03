"""Risk policy: decides a tier for every tool call and what the user must confirm.

Tiers: green runs immediately; yellow asks ("Should I ...?", voice is fine); red always asks, shows the exact
command/path, and needs a non-voice answer (typed, hotkey or tray click). Catastrophic PowerShell additionally needs
the word CONFIRM typed. Admin elevation is a separate, later confirmation. Nothing here ever auto-approves.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from config import ROOT, env, settings
from safety import paths
from safety.confirm import ConfirmRequest
from tools.registry import GREEN, RED, TIER_RANK, YELLOW, Tool

log = logging.getLogger("vex.safety")

Op = str  # "read" | "list" | "write" | "delete"


@dataclass
class Decision:
    tier: str = GREEN
    reason: str = ""
    blocked: bool = False                 # never allowed, no confirmation offered
    second_confirm: bool = False          # catastrophic: typed CONFIRM after the normal confirmation
    elevate: bool = False                 # admin requested: separate elevation confirmation afterwards
    warnings: list[str] = field(default_factory=list)

    def raise_to(self, other: "Decision") -> "Decision":
        """Combine two decisions: the stricter tier wins, flags are OR-ed, reasons are joined."""
        if other.blocked:
            self.blocked = True
        if TIER_RANK[other.tier] > TIER_RANK[self.tier]:
            self.tier = other.tier
        self.second_confirm |= other.second_confirm
        self.elevate |= other.elevate
        if other.reason and other.reason not in self.reason:
            self.reason = f"{self.reason}; {other.reason}" if self.reason else other.reason
        self.warnings += [w for w in other.warnings if w not in self.warnings]
        return self


# ---- path rules -------------------------------------------------------------------------------------------------
def _env_path(name: str) -> str | None:
    v = os.environ.get(name)
    return paths.canonical(v) if v else None


def safe_roots() -> list[str]:
    """Folders where file tools run without asking."""
    from config import ROOT as _root

    roots = [paths.known_folder("desktop"), paths.known_folder("documents") / "Vex", paths.known_folder("downloads"),
             paths.known_folder("pictures") / "Vex", _root / settings["paths"]["vault"],
             *(Path(p) for p in settings["safety"]["extra_safe_folders"])]
    return [paths.canonical(r) for r in roots]


def critical_roots() -> list[str]:
    """Whole-folder targets that may never be deleted or moved by a tool."""
    out = [paths.canonical(r) for r in (paths.known_folder("desktop"), paths.known_folder("documents"),
                                         paths.known_folder("downloads"), paths.known_folder("pictures"), Path.home())]
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "SystemDrive"):
        if p := _env_path(name):
            out.append(p if name != "SystemDrive" else p.rstrip("\\") + "\\")
    drive = os.environ.get("SystemDrive", "C:") + "\\"
    out.append(os.path.normcase(drive))
    out.append(paths.canonical(Path(drive) / "Users"))
    return list(dict.fromkeys(out))


def protected_roots() -> list[tuple[str, str]]:
    """(canonical root, reason): always red, even for plain reads/lists."""
    out: list[tuple[str, str]] = []
    for name, why in (("SystemRoot", "Windows system folder"), ("ProgramFiles", "Program Files"),
                      ("ProgramFiles(x86)", "Program Files"), ("ProgramW6432", "Program Files")):
        if p := _env_path(name):
            out.append((p, why))
    drive = os.environ.get("SystemDrive", "C:") + "\\"
    for sub in ("Recovery", "System Volume Information", "$Recycle.Bin", "Boot"):
        out.append((paths.canonical(Path(drive) / sub), "system folder"))
    local, roam = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    for base, sub in ((local, r"Google\Chrome"), (local, r"Microsoft\Edge"), (local, r"BraveSoftware"),
                      (local, r"Vivaldi"), (local, r"Opera Software"), (local, r"Chromium"),
                      (roam, r"Mozilla\Firefox"), (roam, r"Opera Software"), (local, r"Mozilla\Firefox")):
        if base:
            out.append((paths.canonical(Path(base) / sub), "browser profile"))
    out.append((paths.canonical(ROOT / settings["paths"]["vault"] / ".vexmind"), "memory index"))
    return out


SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube")
SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.pfx", "*.p12", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*",
                        ".netrc", "credentials*.json", "*-key.json", "*service-account*.json", "*serviceaccount*.json",
                        "login data", "cookies", "web data", "*.kdbx", "*.ppk")


def is_secret_file(canon: str) -> bool:
    name = canon.rsplit("\\", 1)[-1]
    if name == ".env.example":
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_FILE_PATTERNS)


def audit_log_path() -> str:
    return paths.canonical(ROOT / settings["paths"]["logs"] / "actions.jsonl")


def path_decision(raw: str, op: Op) -> Decision:
    """Tier for touching `raw` with `op` (read, list, write, delete)."""
    s = str(raw)
    if s.startswith("\\\\.\\") or s.lower().startswith("\\\\?\\globalroot"):
        return Decision(RED, "device path", blocked=True)
    if paths.has_stream(s):
        return Decision(RED, "NTFS alternate data stream", blocked=True)
    canon = paths.canonical(s)
    parts = set(canon.split("\\"))
    if canon == audit_log_path() and op != "read":
        return Decision(RED, "the audit log is append-only", blocked=True)
    if op in ("delete", "write") and canon in critical_roots():
        return Decision(RED, "this is a whole system or personal folder", blocked=True) if op == "delete" else Decision(RED, "protected location")
    if op == "read" and (is_secret_file(canon) or parts & set(SECRET_DIRS)):
        return Decision(RED, "secret or credential file", blocked=True)
    if parts & set(SECRET_DIRS):
        return Decision(RED, "credentials folder")
    if canon == paths.canonical(Path.home()):
        return Decision(RED, "user profile root")
    for root, why in protected_roots():
        if paths.is_within(canon, root):
            return Decision(RED, why)
    if canon.startswith("\\\\"):
        return Decision(YELLOW, "network location")
    if is_secret_file(canon):
        return Decision(RED, "secret or credential file")
    if any(paths.is_within(canon, r) for r in safe_roots()):
        return Decision(GREEN)
    return Decision(YELLOW, "outside the safe folders")


def paths_decision(items: list[tuple[str, Op]]) -> Decision:
    d = Decision()
    for raw, op in items:
        d.raise_to(path_decision(raw, op))
    return d


# ---- tool classification -----------------------------------------------------------------------------------------
def classify(tool: Tool, args: dict) -> Decision:
    """Final decision for a prepared call: the stricter of the tool's base tier and its argument-based rules."""
    d = Decision(tool.tier)
    if tool.classify is not None:
        try:
            d.raise_to(tool.classify(args))
        except Exception:  # noqa: BLE001 - if we can't judge it, treat it as dangerous
            log.exception("classify failed for %s; treating as red", tool.name)
            d.raise_to(Decision(RED, "could not classify this call"))
    return d


# ---- what the user is asked ---------------------------------------------------------------------------------------
def confirm_requests(tool: Tool, args: dict, decision: Decision) -> list[ConfirmRequest]:
    """The ordered confirmations needed before running. Empty for green."""
    if decision.tier == GREEN:
        return []
    exact = tool.describe(args) if tool.describe else f"{tool.name} {args}"
    timeout = float(settings["safety"]["confirm_timeout"])
    typed_timeout = float(settings["safety"]["typed_timeout"])
    red = decision.tier == RED
    warnings = list(decision.warnings)
    if decision.reason:
        warnings.insert(0, decision.reason)
    reqs = [ConfirmRequest(tool=tool.name, tier=decision.tier, title=("Should I do this?" if not red else "Confirm this action"),
                           detail=exact, warnings=warnings, requires_non_voice=red, timeout=timeout, step="action")]
    if decision.second_confirm:
        reqs.append(ConfirmRequest(tool=tool.name, tier=RED, title="This command is destructive and hard to undo",
                                   detail=exact, warnings=warnings, kind="typed", typed_word="CONFIRM",
                                   requires_non_voice=True, timeout=typed_timeout, step="catastrophic"))
    if decision.elevate:
        reqs.append(ConfirmRequest(tool=tool.name, tier=RED, title="Run this as administrator?",
                                   detail="Windows will show a UAC prompt. The same command, shown above, runs with "
                                          "full administrator rights.\n" + exact,
                                   warnings=[], requires_non_voice=True, timeout=timeout, step="elevation"))
    return reqs


def dry_run_enabled() -> bool:
    return env("DRY_RUN").lower() in ("1", "true", "yes")
