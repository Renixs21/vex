"""Windows path handling shared by tools and the safety policy: known folders, resolution, canonical comparison."""
from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path

SEP = "\\"
_FOLDERS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
}


class _GUID(ctypes.Structure):
    _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]


def known_folder(name: str) -> Path:
    """Path of a Windows known folder (follows OneDrive redirection); falls back to ~/<Name>."""
    fallback = Path.home() / name.capitalize()
    try:
        guid = _GUID()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + _FOLDERS[name] + "}"), ctypes.byref(guid))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            p = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return p
    except Exception:  # noqa: BLE001
        pass
    return fallback


def resolve_path(raw: str) -> Path:
    """Expand ~, env vars, and a leading Desktop/Documents/Downloads/Pictures segment; bare names go to Documents/Vex."""
    raw = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    p = Path(raw)
    if not p.is_absolute():
        parts = p.parts
        if parts and parts[0].lower() in _FOLDERS:
            return known_folder(parts[0].lower()).joinpath(*parts[1:])
        return known_folder("documents") / "Vex" / p
    return p


def _long_name(p: str) -> str:
    """Expand 8.3 short names (PROGRA~1) for the part of the path that exists."""
    try:
        import win32api

        probe = p
        tail: list[str] = []
        while probe and not os.path.exists(probe):
            parent, name = os.path.split(probe)
            if parent == probe:
                break
            tail.append(name)
            probe = parent
        if probe and os.path.exists(probe):
            probe = win32api.GetLongPathName(probe)
        return os.path.join(probe, *reversed(tail)) if tail else probe
    except Exception:  # noqa: BLE001
        return p


def canonical(raw: str | Path) -> str:
    """Absolute, symlink-free, long-name, case-folded path with `..` and trailing dots/spaces removed.

    This is what safety decisions compare, so tricks like `..` climbing out of a safe folder, junctions, 8.3 short
    names (PROGRA~1), a trailing dot after a folder name, or different casing all land on the same string.
    """
    s = str(resolve_path(str(raw))).replace("/", SEP)
    if s.startswith("\\\\?\\") and not s.startswith("\\\\?\\UNC"):
        s = s[4:]
    # Windows ignores trailing dots/spaces in each path component
    s = SEP.join(c.rstrip(". ") if c not in (".", "..") else c for c in s.split(SEP))
    s = os.path.realpath(os.path.normpath(s))
    s = _long_name(s)
    return os.path.normcase(os.path.normpath(s))


def has_stream(raw: str | Path) -> bool:
    """True for NTFS alternate data streams like `file.txt:hidden`."""
    s = str(raw).strip().strip('"')
    rest = s[2:] if re.match(r"^[A-Za-z]:", s) else s
    return ":" in rest


def is_within(path: str, root: str) -> bool:
    """Canonical `path` equals or lies under canonical `root`."""
    return path == root or path.startswith(root.rstrip(SEP) + SEP)
