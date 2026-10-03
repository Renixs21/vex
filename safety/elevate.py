"""Administrator actions through the Windows UAC prompt. Never used at startup, never automatic.

The executor asks the user to confirm the command first, then asks separately whether to elevate; only then is this
module called, and Windows itself shows the UAC dialog. The command runs as -EncodedCommand built here from the exact
text the user approved (so there is no script file that could be swapped between approval and execution).
"""
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("vex.elevate")
SEE_MASK_NOCLOSEPROCESS = 0x40
SW_HIDE = 0
ERROR_CANCELLED = 1223
MAX_ENCODED = 30000


def build_encoded(command: str, out_file: Path) -> str:
    """Base64 (UTF-16LE) of a wrapper that runs `command` and sends all its output to `out_file`."""
    target = str(out_file).replace(chr(39), chr(39) * 2)
    inner = (f"$ErrorActionPreference = 'Continue'; & {{ {command}\n}} *>&1 | Out-String -Width 200 | "
             f"Out-File -FilePath '{target}' -Encoding utf8; exit $LASTEXITCODE")
    return base64.b64encode(inner.encode("utf-16-le")).decode("ascii")


def _shell_execute(file: str, params: str) -> int:
    """ShellExecuteEx with the 'runas' verb; returns a process handle (patched in tests)."""
    from win32com.shell import shell, shellcon

    info = shell.ShellExecuteEx(fMask=SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=file, lpParameters=params,
                                nShow=SW_HIDE)
    return info["hProcess"]


def _wait(handle: int, timeout_ms: int) -> tuple[bool, int | None]:
    import win32api
    import win32event
    import win32process

    if win32event.WaitForSingleObject(handle, timeout_ms) == win32event.WAIT_TIMEOUT:
        return False, None
    code = win32process.GetExitCodeProcess(handle)
    win32api.CloseHandle(handle)
    return True, code


def current_user_sid() -> str:
    """SID string of the user this (non-elevated) process runs as."""
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])


def _make_output_dir() -> Path:
    """A private folder the elevated process can write to and *this* process can still read.

    mkdtemp's folder is only open to SYSTEM, Administrators and its owner; a file the elevated process creates there is
    owned by Administrators, and our normal (filtered) token can't read it. Granting our own SID inheritable access on the
    folder fixes that, while keeping it closed to other users.
    """
    workdir = Path(tempfile.mkdtemp(prefix="vex_admin_"))
    r = subprocess.run(["icacls", str(workdir), "/grant", f"*{current_user_sid()}:(OI)(CI)F"],
                       capture_output=True, text=True, check=False)
    if r.returncode:
        shutil.rmtree(workdir, ignore_errors=True)
        raise OSError(f"could not prepare a private folder for the output: {(r.stdout + r.stderr).strip()}")
    return workdir


def run_powershell_elevated(command: str, timeout: int = 60) -> tuple[bool, str]:
    """Run `command` as administrator. Returns (ok, output or error message)."""
    try:
        workdir = _make_output_dir()
    except OSError as e:
        return False, f"Couldn't start the command as administrator: {e}"
    out_file = workdir / "output.txt"
    encoded = build_encoded(command, out_file)
    if len(encoded) > MAX_ENCODED:
        shutil.rmtree(workdir, ignore_errors=True)
        return False, "That command is too long to run as administrator."
    try:
        handle = _shell_execute("powershell.exe", f"-NoProfile -NonInteractive -EncodedCommand {encoded}")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        if getattr(e, "winerror", None) == ERROR_CANCELLED or (e.args and e.args[0] == ERROR_CANCELLED):
            return False, "The administrator prompt was cancelled, so nothing ran."
        log.exception("elevation failed")
        return False, f"Couldn't start the command as administrator: {e}"
    finished, code = _wait(handle, timeout * 1000)
    try:
        out = out_file.read_text(encoding="utf-8-sig", errors="replace") if out_file.exists() else ""   # PS 5.1 adds a BOM
    except OSError as e:
        out = ""
        log.warning("could not read the administrator command's output: %s", e)
    if not finished:                                    # still running: leave the folder, the process may still write
        return False, f"The command was still running after {timeout} seconds; I can't stop an administrator process. {out[-500:]}"
    shutil.rmtree(workdir, ignore_errors=True)
    return code == 0, (out.strip() or f"(no output, exit code {code})")
