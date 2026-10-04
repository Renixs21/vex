"""Start Vex when you log in to Windows.

    python -m ui.autostart --status | --on | --off

A per-user entry in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (no admin, nothing system-wide). It launches the supervisor
(restarts Vex if it crashes: 3 times in 10 minutes, never after a clean Quit) after a 15 s delay, quietly: tray only, orb parked as the
edge pill, no toast, no sound.

Rules (kept in data/autostart.json):
  * The first time Vex runs after this feature exists it turns itself on, once, and tells you with ONE toast.
  * `--off` / the tray toggle removes the entry completely and records "off". From then on Vex never turns it on again, however
    many times it is updated or restarted. Only `--on` or the tray toggle can.
  * If the entry is later removed by something else (e.g. you deleted it in the registry), that is respected as "off" too.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("vex.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Vex"
DELAY_SECONDS = 15


class RegistryBackend:
    """HKCU Run key through winreg."""

    def get(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                return str(winreg.QueryValueEx(k, VALUE_NAME)[0])
        except FileNotFoundError:
            return None

    def set(self, command: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        import winreg

        for key in (RUN_KEY, APPROVED_KEY):                     # the Run value, and Task Manager's on/off record of it
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass


_backend = RegistryBackend()


def set_backend(backend) -> None:
    """Tests swap in a fake so nothing ever touches the real registry."""
    global _backend
    _backend = backend


def _root() -> Path:
    from config import ROOT

    return ROOT


_state_path: Path | None = None


def state_path() -> Path:
    return _state_path or _root() / "data" / "autostart.json"


def set_state_path(path: Path | None) -> None:
    global _state_path
    _state_path = path


def command(python: str | None = None, root: Path | None = None) -> str:
    """The exact command stored in the registry: pythonw (no console window) -> main.py --supervise (the supervisor)."""
    root = Path(root) if root else _root()
    exe = Path(python or sys.executable)
    pyw = exe.with_name("pythonw.exe")
    exe = pyw if pyw.exists() else exe
    return f'"{exe}" "{root / "main.py"}" --supervise --startup-delay {DELAY_SECONDS} --quiet'


def read_state() -> str | None:
    """'on', 'off', or None (never decided)."""
    try:
        return json.loads(state_path().read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError):
        return None


def _write_state(state: str) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"state": state}), encoding="utf-8")


def is_enabled() -> bool:
    return _backend.get() is not None


def enable() -> str:
    cmd = command()
    _backend.set(cmd)
    _write_state("on")
    log.info("autostart on: %s", cmd)
    return cmd


def disable() -> bool:
    """Remove the entry completely and remember the choice. True if it is really gone."""
    _backend.delete()
    _write_state("off")
    gone = _backend.get() is None
    log.info("autostart off (entry %s)", "removed" if gone else "STILL PRESENT")
    return gone


def ensure_default() -> str | None:
    """Called at every start of the app. Returns 'enabled' the one time it turned autostart on (the caller shows ONE toast), else None."""
    state, current = read_state(), _backend.get()
    if state == "off":
        if current is not None:                                  # something put it back: the user's "off" still wins
            _backend.delete()
        return None
    if state is None:
        enable()
        return "enabled"
    if current is None:                                          # was on; removed by someone else: respect that, stay off
        _write_state("off")
        log.info("autostart entry was removed outside Vex; treating that as off")
        return None
    if current != command():                                     # moved or updated install: quietly point it at the right place
        _backend.set(command())
        log.info("autostart entry updated to the current install")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ui.autostart", description="Start Vex with Windows.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true", help="start Vex when you log in")
    g.add_argument("--off", action="store_true", help="remove it completely; Vex will never turn it on again by itself")
    g.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.on:
        print("Vex will start when you log in (after 15 s, quietly):")
        print(" ", enable())
    elif a.off:
        if disable():
            print("Autostart removed. Vex will not start with Windows, and will not turn this on again by itself.")
        else:
            print("Couldn't remove the autostart entry. Remove 'Vex' from HKCU\\" + RUN_KEY + " yourself.")
            return 1
    else:
        cur = _backend.get()
        print(f"Autostart is {'ON' if cur else 'OFF'} (remembered choice: {read_state() or 'none yet'})")
        if cur:
            print(" ", cur)
    return 0


if __name__ == "__main__":
    sys.exit(main())
