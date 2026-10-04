"""Apps: open (Start Menu shortcuts), close (politely), install/uninstall (winget, package resolved before asking)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from rapidfuzz import fuzz, process

from safety.policy import Decision
from tools.registry import GREEN, RED, YELLOW, Refuse, ToolResult, tool

WINGET_ALREADY_INSTALLED = (-1978335189, 0x8A15002B)
WINGET_NO_APPLICABLE = (-1978335188, 0x8A15002C)
CORE_PROCESSES = {"system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "svchost", "dwm",
                  "fontdrvhost", "explorer", "sihost", "ctfmon", "msmpeng", "nissrv", "securityhealthservice", "wudfhost",
                  "audiodg", "conhost", "memory compression", "secure system"}


def child_env() -> dict[str, str]:
    """Environment for child processes, without Vex's API keys and credentials."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET")) and k != "GOOGLE_APPLICATION_CREDENTIALS"}


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a command, return (exit code, combined output). Patched in tests."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=child_env(),
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---- open -----------------------------------------------------------------------------------------------------------
def _start_menu_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    return [d for d in dirs if d.is_dir()]


def start_menu_apps() -> dict[str, Path]:
    """Map shortcut display name -> .lnk path."""
    apps: dict[str, Path] = {}
    for d in _start_menu_dirs():
        for lnk in d.rglob("*.lnk"):
            apps.setdefault(lnk.stem, lnk)
    return apps


def find_app(name: str, apps: dict[str, Path] | None = None) -> tuple[str, Path] | None:
    """Best fuzzy match for `name` among Start Menu apps (score >= 70)."""
    apps = apps if apps is not None else start_menu_apps()
    hit = process.extractOne(name, apps.keys(), scorer=fuzz.WRatio, score_cutoff=70)
    return (hit[0], apps[hit[0]]) if hit else None


def _snapshot_windows(name: str) -> None:
    """Remember which windows exist before launching, so 'type into it' later goes to the new window, not an old one."""
    try:
        from safety import context
        from tools import computer

        context.note_launch(name, {w.hwnd for w in computer.backend().windows()})
    except Exception:  # noqa: BLE001 - never let this bookkeeping stop an app from opening
        pass


# Command shells and admin/system tools: opening one is a different kind of action from opening Notepad, so it needs a yes.
_DANGEROUS_APP_WORDS = (
    "cmd", "command prompt", "powershell", "pwsh", "windows terminal", "terminal", "wt", "regedit", "registry editor", "reg",
    "mmc", "msiexec", "mshta", "wscript", "cscript", "diskpart", "bcdedit", "format", "taskkill", "schtasks", "task scheduler",
    "netsh", "wmic", "rundll32", "regsvr32", "certutil", "bitsadmin", "msconfig", "system configuration", "services",
    "gpedit", "group policy", "secpol", "local security policy", "lusrmgr", "compmgmt", "computer management", "diskmgmt",
    "disk management", "devmgmt", "device manager", "eventvwr", "event viewer", "wsl", "bash", "ssh", "telnet", "runas",
    "psexec", "dism", "sfc", "chkdsk", "cipher", "takeown", "icacls", "bcdboot", "conhost", "powershell ise", "debug")
_DANGEROUS_APP_RX = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w) for w in sorted(_DANGEROUS_APP_WORDS, key=len, reverse=True)) + r")(?![\w])", re.I)


def is_dangerous_app(*names: str | None) -> bool:
    """True if the app asked for, or the Start Menu entry it resolves to, is a shell or an admin/system tool."""
    for n in names:
        if not n:
            continue
        n = re.sub(r"\.(?:exe|lnk|msc|cpl)$", "", n.strip(), flags=re.I)
        n = re.sub(r"[^\w\s]", " ", n)
        if _DANGEROUS_APP_RX.search(n):
            return True
    return False


def _classify_open(a: dict) -> Decision:
    hit = find_app(a["name"])
    exe = shutil.which(a["name"]) or shutil.which(a["name"] + ".exe")
    if is_dangerous_app(a["name"], hit[0] if hit else None, Path(exe).stem if exe else None):
        return Decision(YELLOW, "this opens a command shell or an admin/system tool")
    return Decision(GREEN)


@tool("open_app", "Open an installed app by name.",
      {"name": {"type": "string", "description": "App name as spoken"}}, describe=lambda a: f"Open app: {a['name']}",
      classify=_classify_open)
def open_app(name: str) -> ToolResult:
    _snapshot_windows(name)
    match = find_app(name)
    if match:
        label, lnk = match
        os.startfile(lnk)  # noqa: S606
        return ToolResult(True, f"Opened {label}.")
    exe = shutil.which(name) or shutil.which(name + ".exe")
    if exe:
        subprocess.Popen([exe], env=child_env())  # noqa: S603
        return ToolResult(True, f"Opened {name}.")
    return ToolResult(False, f"Couldn't find an app called '{name}'. It may need installing.")


# ---- close ----------------------------------------------------------------------------------------------------------
def _find_processes(name: str) -> list[tuple[int, str]]:
    import psutil

    want = re.sub(r"\.exe$", "", name.strip().lower())
    me = {os.getpid(), os.getppid()}
    user = None
    try:
        user = psutil.Process().username()
    except psutil.Error:
        pass
    found: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            pname = (p.info["name"] or "").lower()
            stem = re.sub(r"\.exe$", "", pname)
            if p.info["pid"] in me or (user and p.info["username"] and p.info["username"] != user):
                continue
            if stem == want or (len(want) >= 4 and fuzz.ratio(stem, want) >= 85):
                found.append((p.info["pid"], pname))
        except psutil.Error:
            continue
    return found


def _prepare_close(a: dict) -> dict:
    procs = _find_processes(a["name"])
    if not procs:
        raise Refuse(f"I don't see '{a['name']}' running.")
    core = [n for _p, n in procs if re.sub(r"\.exe$", "", n) in CORE_PROCESSES]
    if core:
        raise Refuse(f"{core[0]} is a core Windows process; I won't close it.")
    return {**a, "pids": [p for p, _n in procs], "process_names": sorted({n for _p, n in procs})}


@tool("close_app", "Close a running app politely.",
      {"name": {"type": "string", "description": "App/process name, e.g. notepad"}}, tier=YELLOW,
      prepare=_prepare_close,
      describe=lambda a: f"Close {', '.join(a['process_names'])}  ({len(a['pids'])} process(es), PIDs {', '.join(map(str, a['pids']))})")
def close_app(name: str, pids: list[int] | None = None, process_names: list[str] | None = None) -> ToolResult:
    import psutil

    pids = pids if pids is not None else [p for p, _n in _find_processes(name)]
    if not pids:
        return ToolResult(False, f"I don't see '{name}' running.")
    for pid in pids:
        _run(["taskkill", "/PID", str(pid)], 10)                      # no /F: a polite close request
    _gone, alive = psutil.wait_procs([p for p in (_proc(pid) for pid in pids) if p], timeout=3)
    if alive:
        return ToolResult(False, f"Asked {name} to close, but it's still running (maybe waiting on unsaved work). I didn't force it.")
    return ToolResult(True, f"Closed {name}.")


def _proc(pid: int):
    import psutil

    try:
        return psutil.Process(pid)
    except psutil.Error:
        return None


# ---- winget ---------------------------------------------------------------------------------------------------------
def _winget_available() -> bool:
    return shutil.which("winget") is not None


def parse_winget_table(text: str) -> list[dict]:
    """Parse `winget search/list` output into [{'name','id','version','source'}]."""
    lines = [l.rstrip() for l in re.split(r"[\r\n]+", text) if l.strip()]
    for i, line in enumerate(lines):
        if i > 0 and re.fullmatch(r"-{5,}", line.strip()):
            header = lines[i - 1]
            cols = [(m.group(0).lower(), m.start()) for m in re.finditer(r"\S+", header)]
            rows = []
            for row in lines[i + 1:]:
                rec: dict[str, str] = {}
                for j, (col, start) in enumerate(cols):
                    end = cols[j + 1][1] if j + 1 < len(cols) else len(row)
                    rec[col] = row[start:end].strip() if start < len(row) else ""
                if rec.get("id"):
                    rows.append({"name": rec.get("name", ""), "id": rec["id"], "version": rec.get("version", ""),
                                 "source": rec.get("source") or "winget"})
            return rows
    return []


def choose_package(query: str, rows: list[dict]) -> dict:
    """Pick the package for `query`, or raise Refuse listing candidates when it's unclear."""
    if not rows:
        raise Refuse(f"I couldn't find '{query}' in winget.")
    scored = sorted(((100 if r["name"].casefold() == query.casefold() or r["id"].casefold() == query.casefold()
                      else fuzz.WRatio(query, r["name"]), r) for r in rows), key=lambda t: -t[0])
    top_score, top = scored[0]
    options = ", ".join(f"{r['name']} ({r['id']})" for _s, r in scored[:3])
    if top_score < 70:
        raise Refuse(f"I'm not sure which package you mean by '{query}'. Closest: {options}. Ask the user which one.")
    if len(scored) > 1 and top_score < 100 and scored[1][0] >= top_score - 3 and scored[1][1]["id"] != top["id"]:
        raise Refuse(f"'{query}' matches more than one package: {options}. Ask the user which one.")
    return top


def _publisher(pkg: dict) -> str:
    try:
        _rc, out = _run(["winget", "show", "--id", pkg["id"], "--exact", "--source", pkg["source"],
                         "--accept-source-agreements"], 60)
    except (OSError, subprocess.TimeoutExpired):
        return "(unknown)"
    m = re.search(r"^\s*Publisher:\s*(.+)$", out, re.M)
    return m.group(1).strip() if m else "(unknown)"


def _resolve(verb: str):
    def prepare(a: dict) -> dict:
        if not _winget_available():
            raise Refuse("winget isn't available on this PC.")
        cmd = ["winget", "search", a["name"], "--accept-source-agreements"] if verb == "install" else \
              ["winget", "list", "--name", a["name"], "--accept-source-agreements"]
        try:
            rc, out = _run(cmd, 60)
        except subprocess.TimeoutExpired:
            raise Refuse("winget took too long to answer.") from None
        pkg = choose_package(a["name"], parse_winget_table(out))
        return {**a, "package_id": pkg["id"], "package_name": pkg["name"], "source": pkg["source"],
                "version": pkg["version"], "publisher": _publisher(pkg)}
    return prepare


def _describe(verb: str):
    return lambda a: (f"{verb.capitalize()} app: {a['package_name']}\n  ID:        {a['package_id']}\n"
                      f"  Publisher: {a['publisher']}\n  Source:    {a['source']}\n  Version:   {a['version'] or '(latest)'}")


def _winget_run(args: list[str], package_id: str, name: str, verb: str) -> ToolResult:
    try:
        rc, out = _run(["winget", *args], 900)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"{verb} of {name} timed out after 15 minutes.")
    tail = out.strip()[-400:]
    if rc == 0:
        return ToolResult(True, f"{'Installed' if verb == 'install' else 'Uninstalled'} {name}.")
    if rc in WINGET_ALREADY_INSTALLED or rc in WINGET_NO_APPLICABLE:
        return ToolResult(True, f"{name} is already installed.")
    return ToolResult(False, f"winget couldn't {verb} {name} (exit code {rc}). {tail}")


@tool("install_app", "Install an app with winget; the user approves the exact package.",
      {"name": {"type": "string", "description": "App name, e.g. 'VLC' or 'Spotify'"}}, tier=YELLOW,
      prepare=_resolve("install"), describe=_describe("install"))
def install_app(name: str, package_id: str | None = None, package_name: str | None = None, source: str | None = None,
                version: str | None = None, publisher: str | None = None) -> ToolResult:
    if not package_id:
        return ToolResult(False, "No package was resolved.")
    return _winget_run(["install", "--id", package_id, "--exact", "--source", source or "winget", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"], package_id, package_name or name, "install")


@tool("uninstall_app", "Uninstall an app with winget; the user approves.",
      {"name": {"type": "string", "description": "App name"}}, tier=YELLOW,
      prepare=_resolve("uninstall"), describe=_describe("uninstall"))
def uninstall_app(name: str, package_id: str | None = None, package_name: str | None = None, source: str | None = None,
                  version: str | None = None, publisher: str | None = None) -> ToolResult:
    if not package_id:
        return ToolResult(False, "No package was resolved.")
    return _winget_run(["uninstall", "--id", package_id, "--exact", "--silent", "--accept-source-agreements"],
                       package_id, package_name or name, "uninstall")
