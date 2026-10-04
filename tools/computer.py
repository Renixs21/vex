"""Computer control: open links, windows, and typing/pressing keys in an app.

Safety, in short (details in docs/TOOLS.md):
  * open_url: http/https only. Green when the user named the site in their own message; otherwise yellow, with the URL shown.
  * Windows tools: list, focus, minimize, maximize, snap and show-desktop are green; closing a window is yellow.
  * type_text / press_keys: always yellow, showing the exact text or keys (and the window). If the turn has read outside content
    (a web page, a file, the clipboard, window titles) the request becomes on-screen-only with a warning that the text may not be the
    user's own. Typing into a command shell, the Run box or a registry editor is refused: that would bypass run_powershell, which
    shows the exact command and asks. Win+R and Win+X are refused for the same reason.
All Windows calls go through a small backend object so tests can run the tools against a fake one.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from urllib.parse import urlsplit

from safety import context
from safety.policy import Decision
from tools.registry import GREEN, RED, YELLOW, Refuse, ToolResult, tool

log = logging.getLogger("vex.computer")

MAX_TYPE_CHARS = 1000
MAX_KEY_COMBOS = 6
SHELL_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "conhost.exe", "openconsole.exe",
                   "regedit.exe", "mmc.exe", "powershell_ise.exe", "wsl.exe", "bash.exe", "wslhost.exe"}
FOCUS_WAIT = 5.0


# ---- windows backend -------------------------------------------------------------------------------------------------------
@dataclass
class Window:
    hwnd: int
    title: str
    process: str
    pid: int = 0
    cls: str = ""

    @property
    def label(self) -> str:
        app = re.sub(r"\.exe$", "", self.process, flags=re.I)
        return f"{self.title} ({app})" if app else self.title


def is_shell_window(w: Window | None) -> bool:
    """A window where typed text would run as a command (shell, Run box, registry editor...)."""
    if w is None:
        return False
    return w.process.lower() in SHELL_PROCESSES or (w.cls == "#32770" and w.title.strip().lower() == "run")


VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B, "super": 0x5B}
NAMED_KEYS = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
              "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
              "left": 0x25, "right": 0x27, "insert": 0x2D, **{f"f{i}": 0x6F + i for i in range(1, 13)}}
BLOCKED_KEYS_WITH_WIN = ("r", "x")             # Win+R (Run box) and Win+X (power menu with admin shells)


def parse_keys(spec: str) -> list[tuple[list[str], int, str]]:
    """'ctrl+s' or 'ctrl+a ctrl+c' -> [(modifiers, virtual key, display)]. Raises Refuse for anything unknown or blocked."""
    combos = [c for c in re.split(r"[\s,]+", (spec or "").strip()) if c]
    if not combos:
        raise Refuse("No keys given.")
    if len(combos) > MAX_KEY_COMBOS:
        raise Refuse(f"At most {MAX_KEY_COMBOS} key combinations at a time.")
    out = []
    for combo in combos:
        parts = [p.lower() for p in combo.split("+") if p]
        if not parts:
            raise Refuse(f"I don't understand the keys '{combo}'.")
        *mods, key = parts
        mods = ["win" if m in ("windows", "super") else ("ctrl" if m == "control" else m) for m in mods]
        if any(m not in ("ctrl", "alt", "shift", "win") for m in mods):
            raise Refuse(f"I don't know the modifier in '{combo}'. Use ctrl, alt, shift or win.")
        if key in NAMED_KEYS:
            vk = NAMED_KEYS[key]
        elif len(key) == 1 and key.isalnum():
            vk = ord(key.upper())
        else:
            raise Refuse(f"I don't know the key '{key}'.")
        if "win" in mods and key in BLOCKED_KEYS_WITH_WIN:
            raise Refuse(f"{'+'.join(parts)} opens a command launcher, so I won't press it. Ask me to run the command instead; "
                         "you will see exactly what runs.")
        shown = "+".join([m.capitalize() for m in mods] + [key.upper() if len(key) == 1 else key.capitalize()])
        out.append((mods, vk, shown))
    return out


class Win32Backend:
    """The real thing: ctypes calls into user32. Tests replace it with a fake (set_backend)."""

    def __init__(self) -> None:
        self.u = ctypes.windll.user32
        self.u.GetForegroundWindow.restype = wintypes.HWND
        self.u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u.IsWindowVisible.argtypes = [wintypes.HWND]
        self.u.IsIconic.argtypes = [wintypes.HWND]
        self.u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.IsWindow.argtypes = [wintypes.HWND]

    # -- reading windows
    def _info(self, hwnd) -> Window | None:
        import psutil

        buf = ctypes.create_unicode_buffer(512)
        self.u.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        cbuf = ctypes.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, cbuf, 256)
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value).name()
        except Exception:  # noqa: BLE001
            proc = ""
        return Window(int(hwnd or 0), title, proc, pid.value, cbuf.value)

    def _cloaked(self, hwnd) -> bool:
        v = ctypes.c_int(0)
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(v), ctypes.sizeof(v))
        except Exception:  # noqa: BLE001
            return False
        return v.value != 0

    def windows(self) -> list[Window]:
        """Visible top-level app windows, top of the z-order first. Vex's own windows and the shell's are left out."""
        found: list[Window] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            try:
                if not self.u.IsWindowVisible(hwnd) or self._cloaked(int(hwnd)):
                    return True
                ex = self.u.GetWindowLongW(hwnd, -20)
                if ex & 0x80 and not ex & 0x40000:                # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW
                    return True
                w = self._info(hwnd)
                if w is None or not w.title.strip() or w.pid == os.getpid() or w.title in ("Program Manager", "Vex", "Windows Input Experience")                         or w.title.startswith("Pop-upHost"):
                    return True
                rect = wintypes.RECT()
                self.u.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.right - rect.left < 20 or rect.bottom - rect.top < 20:       # invisible helper windows
                    return True
                found.append(w)
            except Exception:  # noqa: BLE001
                log.debug("window enumeration skipped one window", exc_info=True)
            return True

        self.u.EnumWindows(EnumProc(cb), 0)
        return found

    def foreground(self) -> Window | None:
        h = self.u.GetForegroundWindow()
        return self._info(h) if h else None

    def exists(self, hwnd: int) -> bool:
        return bool(self.u.IsWindow(hwnd))

    # -- acting on windows
    def focus(self, hwnd: int) -> bool:
        if self.u.IsIconic(hwnd):
            self.u.ShowWindow(hwnd, 9)                            # SW_RESTORE
        self.u.keybd_event(0x12, 0, 0, 0)                         # a tap of Alt lets SetForegroundWindow through the foreground lock
        self.u.keybd_event(0x12, 0, 2, 0)
        self.u.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return int(self.u.GetForegroundWindow() or 0) == hwnd

    def show(self, hwnd: int, how: str) -> None:
        self.u.ShowWindow(hwnd, {"minimize": 6, "maximize": 3, "restore": 9}[how])

    def snap(self, hwnd: int, position: str) -> None:
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("monitor", RECT), ("work", RECT), ("flags", wintypes.DWORD)]

        self.u.MonitorFromWindow.restype = wintypes.HANDLE
        mon = self.u.MonitorFromWindow(hwnd, 2)                   # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        self.u.GetMonitorInfoW(mon, ctypes.byref(mi))
        x, y, w, h = mi.work.l, mi.work.t, mi.work.r - mi.work.l, mi.work.b - mi.work.t
        halves = {"left": (x, y, w // 2, h), "right": (x + w // 2, y, w - w // 2, h), "top": (x, y, w, h // 2),
                  "bottom": (x, y + h // 2, w, h - h // 2), "top_left": (x, y, w // 2, h // 2),
                  "top_right": (x + w // 2, y, w - w // 2, h // 2), "bottom_left": (x, y + h // 2, w // 2, h - h // 2),
                  "bottom_right": (x + w // 2, y + h // 2, w - w // 2, h - h // 2)}
        px, py, pw, ph = halves[position]
        self.u.ShowWindow(hwnd, 9)
        self.u.SetWindowPos(hwnd, 0, px, py, pw, ph, 0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE

    def close(self, hwnd: int) -> None:
        self.u.PostMessageW(hwnd, 0x0010, 0, 0)                   # WM_CLOSE: the app can still ask "save?"

    # -- keyboard
    def _send(self, inputs: list) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        sent = self.u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise OSError("Windows blocked the keyboard input (is the target window running as administrator?)")

    def send_text(self, text: str) -> None:
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                self._send([_key(0x0D, False), _key(0x0D, True)])
            elif ch == "\t":
                self._send([_key(0x09, False), _key(0x09, True)])
            else:
                for unit in _utf16_units(ch):
                    self._send([_uni(unit, False), _uni(unit, True)])
            time.sleep(0.004)

    def send_keys(self, combos: list[tuple[list[str], int, str]]) -> None:
        for mods, vk, _ in combos:
            seq = [_key(VK[m], False) for m in mods] + [_key(vk, False), _key(vk, True)] + [_key(VK[m], True) for m in reversed(mods)]
            self._send(seq)
            time.sleep(0.05)

    def open_url(self, url: str) -> bool:
        return webbrowser.open(url)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _key(vk: int, up: bool) -> INPUT:
    ext = 0x0001 if vk in (0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x21, 0x22, 0x23, 0x24, 0x5B) else 0     # KEYEVENTF_EXTENDEDKEY
    return INPUT(type=1, ki=_KEYBDINPUT(vk, 0, ext | (0x0002 if up else 0), 0, 0))


def _uni(unit: int, up: bool) -> INPUT:
    return INPUT(type=1, ki=_KEYBDINPUT(0, unit, 0x0004 | (0x0002 if up else 0), 0, 0))    # KEYEVENTF_UNICODE


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


_backend = None


def backend():
    global _backend
    if _backend is None:
        _backend = Win32Backend()
    return _backend


def set_backend(b) -> None:
    """Tests install a fake backend here (None goes back to the real one)."""
    global _backend
    _backend = b


# ---- finding windows -------------------------------------------------------------------------------------------------------
ACTIVE_WORDS = {"", "active", "current", "this", "foreground", "front", "focused"}


def find_window(query: str, wins: list[Window], foreground: Window | None, exclude: set[int] | None = None) -> Window | None:
    q = (query or "").strip().lower()
    if q in ACTIVE_WORDS:
        return foreground if foreground and not is_vex_window(foreground) else None
    if exclude:
        wins = [w for w in wins if w.hwnd not in exclude]
    stem = lambda w: re.sub(r"\.exe$", "", w.process.lower())  # noqa: E731
    for test in (lambda w: w.title.lower() == q, lambda w: stem(w) == q, lambda w: w.title.lower().startswith(q),
                 lambda w: q in w.title.lower(), lambda w: q in stem(w)):
        for w in wins:                                                       # z-order: the topmost match wins
            if test(w):
                return w
    return None                                                              # no fuzzy guessing: acting on the wrong window is worse than asking


def is_vex_window(w: Window) -> bool:
    return w.title.startswith("Vex") and w.process.lower() in ("pythonw.exe", "python.exe")


def _titles(wins: list[Window], n: int = 8) -> str:
    return "; ".join(w.label for w in wins[:n]) or "none"


# ---- open_url --------------------------------------------------------------------------------------------------------------
def check_url(url: str) -> tuple[str, str]:
    """(clean url, host) or Refuse. http and https only, no credentials in the address, no control characters."""
    u = (url or "").strip()
    if not u or len(u) > 2000 or re.search(r"[\s\x00-\x1f]", u):
        raise Refuse("That isn't a valid web address.")
    if "://" not in u and not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
        u = "https://" + u                                                   # "youtube.com" -> https://youtube.com
    parts = urlsplit(u)
    if parts.scheme.lower() not in ("http", "https"):
        raise Refuse(f"I only open http and https links, not {parts.scheme}:.")
    if not parts.hostname:
        raise Refuse("That web address has no site name.")
    if parts.username or parts.password:
        raise Refuse("I won't open a link that has a username or password in it.")
    return u, parts.hostname.lower()


def _site_words(host: str) -> list[str]:
    labels = [l for l in host.split(".") if l and l != "www"]
    return [l for l in labels[:-1] if len(l) >= 3] or labels                  # 'youtube' from youtube.com, 'bbc' from bbc.co.uk


def user_named_site(url: str, host: str) -> bool:
    """True if the user's own message names this site (the host, the URL, or the site's name)."""
    ctx = context.get()
    text = ctx.user_text.lower()
    if host in text or url.lower() in text or host.removeprefix("www.") in text:
        return True
    return any(context.user_mentions(w) for w in _site_words(host))


def _prepare_url(a: dict) -> dict:
    url, host = check_url(a["url"])
    return {"url": url}


def _classify_url(a: dict):
    url, host = check_url(a["url"])
    named = user_named_site(url, host)
    private = bool(re.match(r"^(localhost|127\.|10\.|192\.168\.|169\.254\.|\[?::1)", host)) or re.match(r"^\d+\.\d+\.\d+\.\d+$", host)
    if named and not private:
        return Decision(GREEN)
    why = "this isn't a site you named" if not named else "this is a local network address"
    return Decision(YELLOW, why)


@tool("open_url", "Open a web link (http or https) in the default browser.",
      {"url": {"type": "string", "description": "Full address, e.g. https://youtube.com"}},
      prepare=_prepare_url, classify=_classify_url, describe=lambda a: f"Open this link in the browser: {a['url']}")
def open_url(url: str) -> ToolResult:
    url, host = check_url(url)
    if not backend().open_url(url):
        return ToolResult(False, "I couldn't open the browser.")
    return ToolResult(True, f"Opened {host}.", {"host": host})


# ---- windows ----------------------------------------------------------------------------------------------------------------
def _prepare_window(a: dict) -> dict:
    b = backend()
    wins = b.windows()
    w = find_window(a.get("window", ""), wins, b.foreground())
    if w is None:
        raise Refuse(f"I can't find a window matching '{a.get('window', '')}'. Open windows: {_titles(wins)}.")
    return {**a, "_hwnd": w.hwnd, "_label": w.label}


def _check_alive(hwnd: int) -> None:
    if not backend().exists(hwnd):
        raise Refuse("That window is gone.")


WINDOW_PARAM = {"window": {"type": "string", "description": "Window title or app name, e.g. 'notepad'; 'active' = current"}}


@tool("list_windows", "List the open windows (title and app).", {})
def list_windows() -> ToolResult:
    wins = backend().windows()
    return ToolResult(True, "\n".join(w.label for w in wins[:30]) or "No open windows.", [w.label for w in wins[:30]])


@tool("focus_window", "Bring a window to the front.", WINDOW_PARAM, prepare=_prepare_window,
      describe=lambda a: f"Bring to front: {a['_label']}")
def focus_window(window: str, _hwnd: int = 0, _label: str = "") -> ToolResult:
    _check_alive(_hwnd)
    ok = backend().focus(_hwnd)
    return ToolResult(ok, f"Brought {_label} to the front." if ok else f"Windows wouldn't bring {_label} to the front.")


def _simple_window_tool(name: str, how: str, text: str, past: str):
    @tool(name, text, WINDOW_PARAM, prepare=_prepare_window, describe=lambda a, t=text: f"{t.split('.')[0]}: {a['_label']}")
    def fn(window: str, _hwnd: int = 0, _label: str = "") -> ToolResult:
        _check_alive(_hwnd)
        backend().show(_hwnd, how)
        return ToolResult(True, f"{past} {_label}.")

    fn.__name__ = name
    return fn


minimize_window = _simple_window_tool("minimize_window", "minimize", "Minimize a window.", "Minimized")
maximize_window = _simple_window_tool("maximize_window", "maximize", "Maximize a window.", "Maximized")
restore_window = _simple_window_tool("restore_window", "restore", "Restore a minimized or maximized window.", "Restored")

SNAP_POSITIONS = ["left", "right", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right"]


@tool("snap_window", "Snap a window to a half or corner of the screen.",
      {**WINDOW_PARAM, "position": {"type": "string", "enum": SNAP_POSITIONS, "description": "Where to put it"}},
      prepare=_prepare_window, describe=lambda a: f"Snap {a['_label']} to the {a['position'].replace('_', ' ')}")
def snap_window(window: str, position: str, _hwnd: int = 0, _label: str = "") -> ToolResult:
    if position not in SNAP_POSITIONS:
        return ToolResult(False, f"Position must be one of: {', '.join(SNAP_POSITIONS)}.")
    _check_alive(_hwnd)
    backend().snap(_hwnd, position)
    return ToolResult(True, f"Snapped {_label} to the {position.replace('_', ' ')}.")


@tool("show_desktop", "Minimize all windows to show the desktop.", {})
def show_desktop() -> ToolResult:
    b = backend()
    wins = [w for w in b.windows() if not is_vex_window(w)]
    for w in wins:
        b.show(w.hwnd, "minimize")
    return ToolResult(True, f"Minimized {len(wins)} window(s).")


@tool("close_window", "Close a window (the app may ask to save first).", WINDOW_PARAM, tier=YELLOW,
      prepare=_prepare_window, describe=lambda a: f"Close the window: {a['_label']}")
def close_window(window: str, _hwnd: int = 0, _label: str = "") -> ToolResult:
    _check_alive(_hwnd)
    backend().close(_hwnd)
    return ToolResult(True, f"Asked {_label} to close.")


# ---- typing and keys ---------------------------------------------------------------------------------------------------------
def _taint_decision(what: str) -> Decision:
    ctx = context.get()
    if ctx.tainted:
        return Decision(RED, f"this {what} may come from content I just read ({', '.join(ctx.untrusted_from)}), not from you. "
                             "Approve on screen only if you recognise it")
    return Decision(YELLOW)


def resolve_target(b, query: str, wait: float = 0.0) -> tuple[Window | None, str]:
    """The window to act on. If this turn just opened the app (open_app), it must be the NEW window: never an older one that may hold
    the user's own text. Returns (window or None, note for the confirmation)."""
    launch = context.launch_for(query)
    end = time.monotonic() + wait
    while True:
        wins, fg = b.windows(), b.foreground()
        if launch is not None:
            w = find_window(query, wins, fg, exclude=launch["before"])
            if w is not None:
                return w, "the window just opened"
        else:
            w = find_window(query, wins, fg)
            if w is not None:
                n = sum(1 for x in wins if find_window(query, [x], fg))
                return w, (f"{n} windows match, using the front-most one" if n > 1 else "")
        if time.monotonic() >= end:
            return None, ("no new window opened" if launch is not None else "")
        time.sleep(0.25)


def _target(a: dict) -> tuple[Window | None, str]:
    b = backend()
    q = a.get("window", "")
    if q.strip():
        return resolve_target(b, q)
    return b.foreground(), ""


def _prepare_input(a: dict) -> dict:
    w, note = _target(a)
    if a.get("window", "").strip() and w is None:
        return {**a, "_label": f"a new '{a['window']}' window (it may take a moment to appear)"}   # found when typing starts
    if is_shell_window(w):
        raise Refuse(f"{w.label} is a command window. Typing there would run commands without showing them first; "
                     "use run_powershell, which shows the exact command and asks.")
    label = w.label if w else "the active window"
    return {**a, "_label": f"{label} [{note}]" if note else label}


def _preview_text(text: str, n: int = 400) -> str:
    return text if len(text) <= n else text[:n] + f"... ({len(text) - n} more characters)"


@tool("type_text", "Type text into an app's window. The user approves the exact text.",
      {"text": {"type": "string", "description": "Exactly what to type (max 1000 characters)"},
       "window": {"type": "string", "description": "Window to type into, e.g. 'notepad' (brought to front). Blank = active", "default": ""}},
      tier=YELLOW,
      prepare=lambda a: _prepare_input(_check_text(a)),
      classify=lambda a: _taint_decision("text"),
      describe=lambda a: f"Type into {a['_label']}: \"{a['text'][:80].replace(chr(10), ' ')}{'...' if len(a['text']) > 80 else ''}\"\n"
                         f"Exact text:\n{_preview_text(a['text'])}")
def type_text(text: str, window: str = "", _label: str = "") -> ToolResult:
    b = backend()
    if window.strip():
        w, note = resolve_target(b, window, FOCUS_WAIT)
        if w is None:
            why = ("It didn't open a new window, and I won't use an older one that may hold your own text. " if note == "no new window opened" else "")
            return ToolResult(False, f"I couldn't find a window matching '{window}'. {why}Nothing was sent.")
        if is_shell_window(w):
            return ToolResult(False, f"{w.label} is a command window; I won't type there.")
        if not b.focus(w.hwnd):
            return ToolResult(False, f"I couldn't bring {w.label} to the front, so I didn't type anything.")
    fg = b.foreground()
    if is_shell_window(fg):                                  # re-check at the moment of typing: the focus may have moved
        return ToolResult(False, f"{fg.label} is in front and it is a command window; I won't type there.")
    try:
        b.send_text(text)
    except OSError as e:
        return ToolResult(False, str(e))
    return ToolResult(True, f"Typed {len(text)} characters into {fg.label if fg else 'the active window'}.")


def _check_text(a: dict) -> dict:
    text = a.get("text", "")
    if not text:
        raise Refuse("There is no text to type.")
    if len(text) > MAX_TYPE_CHARS:
        raise Refuse(f"That's too much to type at once (max {MAX_TYPE_CHARS} characters).")
    return a


def _wait_for_window(b, query: str, wait: float = FOCUS_WAIT) -> Window | None:
    return resolve_target(b, query, wait)[0]


def _prepare_keys(a: dict) -> dict:
    combos = parse_keys(a["keys"])                          # refuses unknown and blocked keys up front
    out = _prepare_input(a)
    return {**out, "_shown": " then ".join(c[2] for c in combos)}


@tool("press_keys", "Press keys or shortcuts (e.g. ctrl+s) in an app. The user approves the exact keys.",
      {"keys": {"type": "string", "description": "e.g. 'ctrl+s', 'alt+tab', 'enter', 'ctrl+a ctrl+c' (up to 6 combos)"},
       "window": {"type": "string", "description": "Window to send them to (brought to front). Blank = active", "default": ""}},
      tier=YELLOW, prepare=_prepare_keys, classify=lambda a: _taint_decision("key press"),
      describe=lambda a: f"Press {a['_shown']} in {a['_label']}")
def press_keys(keys: str, window: str = "", _label: str = "", _shown: str = "") -> ToolResult:
    combos = parse_keys(keys)
    b = backend()
    if window.strip():
        w, note = resolve_target(b, window, FOCUS_WAIT)
        if w is None:
            why = ("It didn't open a new window, and I won't use an older one that may hold your own text. " if note == "no new window opened" else "")
            return ToolResult(False, f"I couldn't find a window matching '{window}'. {why}Nothing was sent.")
        if is_shell_window(w):
            return ToolResult(False, f"{w.label} is a command window; I won't send keys there.")
        if not b.focus(w.hwnd):
            return ToolResult(False, f"I couldn't bring {w.label} to the front, so I didn't press anything.")
    fg = b.foreground()
    if is_shell_window(fg):
        return ToolResult(False, f"{fg.label} is in front and it is a command window; I won't send keys there.")
    try:
        b.send_keys(combos)
    except OSError as e:
        return ToolResult(False, str(e))
    return ToolResult(True, f"Pressed {' then '.join(c[2] for c in combos)}.")
