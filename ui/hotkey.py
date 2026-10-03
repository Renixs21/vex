"""Global hotkey (default Ctrl+Alt+V) through Win32 RegisterHotKey.

RegisterHotKey is the Windows-sanctioned way: the system tells us when the exact combination is pressed, there is no keyboard hook
to be dropped or filtered, and if another program already owns the combination Windows says so (error 1409) instead of failing
silently. It runs on its own thread with a GetMessage loop, because a hotkey belongs to the thread that registered it.

  * Every press is logged ("hotkey pressed").
  * If registration fails the reason is logged and `on_error` is called (the app shows a tray notification).
  * Combination syntax: modifiers ctrl / alt / shift / win, then one key: "ctrl+alt+v", "ctrl+shift+f9", "win+alt+v".
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("vex.hotkey")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
HOTKEY_ID = 0x5645                                   # "VE"

_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN}
_NAMED = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "backspace": 0x08,
          "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
          "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+v' -> (modifier flags, virtual-key code). Raises ValueError for anything RegisterHotKey can't take."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"{combo!r} needs at least one modifier (ctrl, alt, shift, win) and a key")
    *mods, key = parts
    flags = 0
    for m in mods:
        if m not in _MODS:
            raise ValueError(f"unknown modifier {m!r} in {combo!r} (use ctrl, alt, shift or win)")
        flags |= _MODS[m]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED:
        vk = _NAMED[key]
    else:
        raise ValueError(f"unknown key {key!r} in {combo!r}")
    return flags, vk


def explain_error(code: int, combo: str) -> str:
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return (f"{combo} is already taken by another program (or another copy of Vex). Close it, or set a different "
                f"combination under [ui] hotkey in config.toml")
    return f"Windows refused {combo} (error {code})"


class GlobalHotkey:
    def __init__(self, combo: str, callback: Callable[[str], object], on_error: Callable[[str], None] | None = None,
                 user32=None, kernel32=None):
        self.combo = combo
        self.callback = callback
        self.on_error = on_error
        self._user32 = user32
        self._kernel32 = kernel32
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self._ok = False
        self.presses = 0

    # ---- win32 -----------------------------------------------------------------------------------------------------------
    @property
    def u32(self):
        if self._user32 is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            u.RegisterHotKey.restype = wintypes.BOOL
            u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            u.UnregisterHotKey.restype = wintypes.BOOL
            u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u.GetMessageW.restype = ctypes.c_int
            u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostThreadMessageW.restype = wintypes.BOOL
            self._user32 = u
        return self._user32

    @property
    def k32(self):
        if self._kernel32 is None:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return self._kernel32

    @property
    def active(self) -> bool:
        return self._ok and self._thread is not None and self._thread.is_alive()

    # ---- lifecycle -------------------------------------------------------------------------------------------------------
    def start(self) -> bool:
        """Register the hotkey. Returns False after logging the reason and calling on_error."""
        if self.active:
            return True
        try:
            mods, vk = parse_hotkey(self.combo)
        except ValueError as e:
            return self._fail(str(e))
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(target=self._run, args=(mods, vk), name="hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            return self._fail(f"registering {self.combo} timed out")
        if not self._ok:
            return self._fail(self.error or f"could not register {self.combo}")
        log.info("hotkey %s registered (Win32 RegisterHotKey)", self.combo)
        return True

    def _fail(self, reason: str) -> bool:
        self.error = reason
        log.warning("hotkey unavailable: %s", reason)
        if self.on_error:
            try:
                self.on_error(f"Hotkey unavailable: {reason}. Use the tray menu or click the orb to talk.")
            except Exception:  # noqa: BLE001
                log.debug("on_error failed", exc_info=True)
        return False

    def stop(self) -> None:
        t = self._thread
        if t is not None and t.is_alive() and self._tid:
            self.u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            t.join(timeout=2)
        self._ok = False
        self._thread = None

    def _run(self, mods: int, vk: int) -> None:
        user32 = self.u32
        self._tid = self.k32.GetCurrentThreadId()
        registered = False
        try:
            if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                registered = True
                self._ok = True
            else:
                self.error = explain_error(ctypes.get_last_error(), self.combo)
            self._ready.set()
            if not registered:
                return
            msg = wintypes.MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):                                   # WM_QUIT or an error
                    break
                if msg.message == WM_HOTKEY:
                    self.presses += 1
                    log.info("hotkey pressed (%s)", self.combo)
                    try:
                        self.callback("hotkey")
                    except Exception:  # noqa: BLE001 - never let a callback error end the message loop
                        log.exception("hotkey callback failed")
        finally:
            if registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self._ready.set()
            self._ok = False
