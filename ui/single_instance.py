"""Only one Vex app at a time (two would fight over the hotkey, the orb ports and the microphone).

A named Windows mutex is held for the life of the process; Windows releases it by itself if Vex is killed, so there is never a stale
lock to clean up. The owner's PID is written to data/vex.pid purely so the second copy can say who is running.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

log = logging.getLogger("vex.instance")

MUTEX_NAME = "Local\\VexSingleInstance"
ERROR_ALREADY_EXISTS = 183
_handle = None


def _pid_file() -> Path:
    from config import ROOT

    return ROOT / "data" / "vex.pid"


def acquire(name: str = MUTEX_NAME, pid_file: Path | None = None) -> tuple[bool, int | None]:
    """(True, None) if we are the only Vex; (False, pid_of_the_other_if_known) if another copy already runs."""
    global _handle
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateMutexW(None, 0, name)
    pid_file = pid_file or _pid_file()
    if handle and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        try:
            return False, int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return False, None
    if not handle:                                  # couldn't create a mutex at all: don't block startup over it
        log.warning("could not create the single-instance mutex (error %s)", ctypes.get_last_error())
        return True, None
    _handle = handle
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))
    except OSError:
        pass
    return True, None


def release(name_handle=None) -> None:
    """Give the mutex back (tests; the OS does it on exit anyway)."""
    global _handle
    if _handle:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(_handle))
        _handle = None
