import base64
import getpass
import re
import subprocess
from pathlib import Path

import pytest

from safety import elevate


def decoded_target(params: str) -> tuple[Path, str]:
    """What the fake 'elevated process' sees: the output path and the command it was asked to run."""
    enc = re.search(r"-EncodedCommand (\S+)", params).group(1)
    text = base64.b64decode(enc).decode("utf-16-le")
    return Path(re.search(r"-FilePath '([^']+)'", text).group(1).replace("''", "'")), text


def test_output_folder_is_readable_by_the_current_user_but_not_open_to_everyone():
    d = elevate._make_output_dir()
    try:
        acl = subprocess.run(["icacls", str(d)], capture_output=True, text=True).stdout
        assert getpass.getuser().lower() in acl.lower()                     # our own account was granted inheritable access
        assert "(OI)(CI)(F)" in acl and "Everyone" not in acl and "Users:" not in acl.replace("NT AUTHORITY", "")
        (d / "probe.txt").write_text("hi", encoding="utf-8")
        assert (d / "probe.txt").read_text(encoding="utf-8") == "hi"
        inherited = subprocess.run(["icacls", str(d / "probe.txt")], capture_output=True, text=True).stdout
        assert getpass.getuser().lower() in inherited.lower()               # so files created later inherit it
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def test_elevated_output_is_read_back_and_the_folder_is_removed(monkeypatch):
    seen = {}

    def fake_exec(file, params):
        out, text = decoded_target(params)
        seen["dir"], seen["text"], seen["file"] = out.parent, text, file
        out.write_bytes(b"\xef\xbb\xbfTrue\r\n")                            # as Windows PowerShell writes it: UTF-8 with a BOM
        return 1234

    monkeypatch.setattr(elevate, "_shell_execute", fake_exec)
    monkeypatch.setattr(elevate, "_wait", lambda h, ms: (True, 0))
    ok, out = elevate.run_powershell_elevated("Get-Date", 5)
    assert ok and out == "True" and seen["file"] == "powershell.exe"
    assert "& { Get-Date" in seen["text"] and not seen["dir"].exists()


def test_cancelled_uac_prompt_is_reported_and_cleans_up(monkeypatch):
    class Cancelled(Exception):
        winerror = 1223

    dirs = []
    monkeypatch.setattr(elevate, "_shell_execute", lambda f, p: (dirs.append(decoded_target(p)[0].parent), (_ for _ in ()).throw(Cancelled()))[1])
    ok, msg = elevate.run_powershell_elevated("Get-Date", 5)
    assert not ok and "cancelled" in msg and not dirs[0].exists()


def test_still_running_after_the_timeout_is_reported(monkeypatch):
    kept = []
    monkeypatch.setattr(elevate, "_shell_execute", lambda f, p: kept.append(decoded_target(p)[0].parent) or 1)
    monkeypatch.setattr(elevate, "_wait", lambda h, ms: (False, None))
    ok, msg = elevate.run_powershell_elevated("Start-Sleep 99", 1)
    assert not ok and "still running" in msg
    import shutil

    shutil.rmtree(kept[0], ignore_errors=True)


def test_if_the_private_folder_cannot_be_prepared_nothing_is_elevated(monkeypatch):
    monkeypatch.setattr(elevate.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 5, "", "access denied"))
    called = []
    monkeypatch.setattr(elevate, "_shell_execute", lambda f, p: called.append(1))
    ok, msg = elevate.run_powershell_elevated("Get-Date", 5)
    assert not ok and "private folder" in msg and called == []


def test_unreadable_output_does_not_crash(monkeypatch):
    monkeypatch.setattr(elevate, "_shell_execute", lambda f, p: (decoded_target(p)[0].write_text("x"), 1)[1])
    monkeypatch.setattr(elevate, "_wait", lambda h, ms: (True, 0))
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    ok, msg = elevate.run_powershell_elevated("Get-Date", 5)
    assert ok and "no output" in msg


def test_too_long_command_is_refused_without_elevating(monkeypatch):
    called = []
    monkeypatch.setattr(elevate, "_shell_execute", lambda f, p: called.append(1))
    ok, msg = elevate.run_powershell_elevated("Write-Output " + "a" * 30000, 5)
    assert not ok and "too long" in msg and called == []
