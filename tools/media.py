"""YouTube search + playback, and media keys."""
from __future__ import annotations

import ctypes
import webbrowser

from yt_dlp import YoutubeDL

from tools.registry import ToolResult, tool

VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
KEYEVENTF_KEYUP = 0x0002


def search_youtube(query: str) -> tuple[str, str] | None:
    """Return (title, url) of the top YouTube result, or None."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = (info or {}).get("entries") or []
    if not entries:
        return None
    e = entries[0]
    return e.get("title", query), e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"


@tool("play_youtube", "Play the top YouTube result in the browser.",
      {"query": {"type": "string", "description": "What to play, e.g. 'lofi beats'"}},
      describe=lambda a: f"Search YouTube for '{a['query']}' and open the top result in the browser")
def play_youtube(query: str) -> ToolResult:
    hit = search_youtube(query)
    if not hit:
        return ToolResult(False, f"No YouTube results for '{query}'.")
    title, url = hit
    webbrowser.open(url)
    return ToolResult(True, f"Playing '{title}'.", {"url": url})


def press_media_key(vk: int) -> None:
    """Send a media key press (patched in tests)."""
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


@tool("media_control", "Play/pause, next, previous or stop whatever is playing.",
      {"action": {"type": "string", "enum": list(VK), "description": "play_pause = pause or resume; next; previous; stop"}},
      describe=lambda a: f"Media key: {a['action']}")
def media_control(action: str) -> ToolResult:
    if action not in VK:
        return ToolResult(False, f"Unknown media action '{action}'. Use one of: {', '.join(VK)}.")
    press_media_key(VK[action])
    return ToolResult(True, f"Sent {action.replace('_', '/')}.")
