"""Test doubles for tools/computer.py. Tests must NEVER touch the real desktop: conftest installs NoWindows for every test."""
from __future__ import annotations

from tools.computer import Window


class NoWindows:
    """Installed by default (see conftest): any use of the real Windows backend in a test is a bug."""

    def __getattr__(self, name):
        raise AssertionError(f"a test used the real window backend ({name}); install tests.fake_windows.FakeBackend instead")


class FakeBackend:
    def __init__(self, windows=None, foreground=None):
        self.wins: list[Window] = list(windows or [])
        self.fg: Window | None = foreground
        self.typed: list[tuple[int | None, str]] = []      # (window that had focus, text)
        self.keys: list[tuple[int | None, list[str]]] = []
        self.urls: list[str] = []
        self.shown: list[tuple[int, str]] = []
        self.snapped: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.focus_ok = True
        self.fail_input = False

    def add(self, hwnd: int, title: str, process: str, cls: str = "") -> Window:
        w = Window(hwnd, title, process, 1000 + hwnd, cls)
        self.wins.insert(0, w)                              # newest on top of the z-order
        return w

    def windows(self):
        return list(self.wins)

    def foreground(self):
        return self.fg

    def exists(self, hwnd):
        return any(w.hwnd == hwnd for w in self.wins)

    def focus(self, hwnd):
        if self.focus_ok:
            self.fg = next(w for w in self.wins if w.hwnd == hwnd)
        return self.focus_ok

    def show(self, hwnd, how):
        self.shown.append((hwnd, how))

    def snap(self, hwnd, position):
        self.snapped.append((hwnd, position))

    def close(self, hwnd):
        self.closed.append(hwnd)

    def send_text(self, text):
        if self.fail_input:
            raise OSError("blocked")
        self.typed.append((self.fg.hwnd if self.fg else None, text))

    def send_keys(self, combos):
        if self.fail_input:
            raise OSError("blocked")
        self.keys.append((self.fg.hwnd if self.fg else None, [c[2] for c in combos]))

    def open_url(self, url):
        self.urls.append(url)
        return True
