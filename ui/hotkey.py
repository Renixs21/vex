"""Global hotkey (default Win+Alt+V): start listening, or, while Vex is speaking, stop and listen.

Uses the `keyboard` package's low-level hook, which needs no admin rights (it can't see keys typed into elevated
windows, which is a Windows rule). If the hook can't be installed, Vex keeps running: on_error is called once and the tray
"Listen now" item and clicking the visual still work.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("vex.hotkey")


class GlobalHotkey:
    def __init__(self, combo: str, callback: Callable[[str], object], on_error: Callable[[str], None] | None = None,
                 keyboard_module=None):
        self.combo = combo
        self.callback = callback
        self.on_error = on_error
        self._kb = keyboard_module
        self._handle = None

    @property
    def kb(self):
        if self._kb is None:
            import keyboard

            self._kb = keyboard
        return self._kb

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self) -> bool:
        """Install the hotkey. Returns False (after calling on_error) if it can't be installed."""
        if self._handle is not None:
            return True
        try:
            # on release, so holding the keys down doesn't fire repeatedly
            self._handle = self.kb.add_hotkey(self.combo, self._fire, suppress=False, trigger_on_release=True)
            log.info("hotkey %s registered", self.combo)
            return True
        except Exception as e:  # noqa: BLE001 - bad combo, no permission, no keyboard hook...
            log.warning("could not register hotkey %s: %s", self.combo, e)
            self._handle = None
            if self.on_error:
                self.on_error(f"Hotkey {self.combo} is unavailable ({e}). Use the tray menu or click the visual to talk.")
            return False

    def stop(self) -> None:
        if self._handle is not None:
            try:
                self.kb.remove_hotkey(self._handle)
            except Exception:  # noqa: BLE001
                log.debug("remove_hotkey failed", exc_info=True)
            self._handle = None

    def _fire(self) -> None:
        try:
            self.callback("hotkey")
        except Exception:  # noqa: BLE001 - never let a callback error kill the keyboard hook thread
            log.exception("hotkey callback failed")
