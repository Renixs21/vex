"""Tool groups. Sending all 37 tool definitions with every request cost ~2,500 tokens and tripped the free-tier rate
limits, so the LLM is offered a small core set plus only the groups that look relevant:

  * core (always): open_app, play_youtube, web_search, recall, remember, more_tools
  * a group is added when the user's message matches its keywords,
  * or when the model calls more_tools(category),
  * and a group used in the previous turn stays on for the next one ("yes" after "retry as administrator?").

The agent only lets the model call tools it was offered this step; anything else is told to use more_tools.
"""
from __future__ import annotations

import re

from tools import registry

CORE = ("open_app", "open_url", "play_youtube", "web_search", "quick_facts", "recall", "remember", "more_tools", "go_to_sleep")

CATEGORIES: dict[str, dict] = {
    "files": {
        "tools": ["create_file", "read_file", "list_dir", "move_file", "copy_file", "append_to_file", "replace_in_file",
                  "overwrite_file", "delete_file", "undo_last"],
        "summary": "create, read, edit (append, replace, overwrite), list, move, copy, delete files and folders; undo",
        "keywords": ["file", "files", "folder", "folders", "directory", "document", "documents", "desktop", "downloads",
                     "rename", "move", "copy", "delete", "trash", "recycle", "undo", "txt", "bat", "cmd", "ps1", "script",
                     "edit", "append", "replace", "overwrite", "rewrite", "insert", "modify", "add a line", "add to"],
    },
    "computer": {
        "tools": ["type_text", "press_keys", "list_windows", "focus_window", "minimize_window", "maximize_window", "restore_window",
                  "snap_window", "show_desktop", "close_window"],
        "summary": "type text or press keys in an app, and manage windows (minimize, maximize, snap, show desktop, close)",
        "keywords": ["window", "windows", "minimize", "maximize", "snap", "show desktop", "type", "typing", "write", "keyboard",
                     "press", "keys", "shortcut", "ctrl", "enter", "paste", "focus", "switch to", "fullscreen", "full screen"],
    },
    "apps": {
        "tools": ["close_app", "install_app", "uninstall_app"],
        "summary": "close, install, uninstall apps",
        "keywords": ["install", "uninstall", "close", "quit", "app", "apps", "application", "program", "winget", "update"],
    },
    "media": {
        "tools": ["media_control"],
        "summary": "play/pause, next, previous, stop",
        "keywords": ["pause", "resume", "next", "previous", "skip", "song", "songs", "music", "track", "playlist", "stop the"],
    },
    "tasks": {
        "tools": ["add_task", "list_tasks", "complete_task", "edit_task", "delete_task", "set_reminder", "list_reminders",
                  "cancel_reminder", "today_summary"],
        "summary": "to-do list (add, edit, delete, complete) and reminders (one-time, recurring, list, cancel), what's on today",
        "keywords": ["task", "tasks", "todo", "to-do", "to do", "remind", "reminder", "reminders", "checklist", "due", "agenda",
                     "schedule", "what's on", "whats on", "on today", "my day", "recurring", "every day", "every week", "every weekday"],
    },
    "system": {
        "tools": ["set_volume", "mute", "set_brightness", "screenshot", "get_clipboard", "set_clipboard", "system_info", "lock_pc"],
        "summary": "volume, brightness, screenshot, clipboard, system info, lock",
        "keywords": ["volume", "mute", "unmute", "louder", "quieter", "sound", "brightness", "dim", "brighter", "screenshot",
                     "screenshots", "screen shot", "clipboard", "battery", "cpu", "ram", "disk", "storage", "system", "lock"],
    },
    "web": {
        "tools": ["web_fetch", "get_weather"],
        "summary": "read a web page, weather forecast",
        "keywords": ["weather", "forecast", "temperature", "rain", "raining", "snow", "humidity", "umbrella", "how hot", "how cold",
                     "web page", "webpage", "article", "website", "url", "http", "https", "www", "read this page", "this link"],
    },
    "memory": {
        "tools": ["read_note", "link_notes", "list_notes", "forget", "memory_stats"],
        "summary": "read, link, list, forget notes in memory",
        "keywords": ["forget", "memory", "memories", "my notes", "what do you know", "remember about"],
    },
    "shell": {
        "tools": ["run_powershell", "shutdown_pc", "restart_pc", "cancel_shutdown"],
        "summary": "PowerShell, shutdown, restart",
        "keywords": ["powershell", "shell", "command", "terminal", "cmd", "run", "execute", "shutdown", "shut down", "restart",
                     "reboot", "power off", "admin", "administrator", "elevated", "process", "service", "registry", "ipconfig",
                     "ping", "retry", "again"],
    },
}

_PATTERNS = {name: re.compile(r"(?<![\w])(?:" + "|".join(re.escape(k) for k in spec["keywords"]) + r")(?![\w])", re.I)
             for name, spec in CATEGORIES.items()}


def match(text: str) -> set[str]:
    """Categories whose keywords appear in the user's message."""
    return {name for name, rx in _PATTERNS.items() if rx.search(text or "")}


def category_summary() -> str:
    return "; ".join(f"{n} ({s['summary']})" for n, s in CATEGORIES.items())


def apply() -> None:
    """Stamp each registered tool with its category (called once all tool modules are loaded)."""
    for name, spec in CATEGORIES.items():
        for tool_name in spec["tools"]:
            t = registry.get(tool_name)
            if t is not None:
                t.category = name
    for tool_name in CORE:
        t = registry.get(tool_name)
        if t is not None:
            t.category = "core"


@registry.tool("more_tools", "Get more tools by category (the groups are listed in your instructions).",
               {"category": {"type": "string", "enum": list(CATEGORIES), "description": "Tool group"}},
               describe=lambda a: f"Offer the {a['category']} tools")
def more_tools(category: str) -> registry.ToolResult:
    spec = CATEGORIES.get(category)
    if spec is None:
        return registry.ToolResult(False, f"Unknown category '{category}'. Use one of: {', '.join(CATEGORIES)}.")
    return registry.ToolResult(True, f"Added the {category} tools: {', '.join(spec['tools'])}.", {"category": category})


# ---- routing: which messages are simple enough for the small, fast model ------------------------------------------------
SIMPLE_GROUPS = {"system", "media", "apps", "tasks"}
_NOT_SIMPLE = re.compile(r"\b(?:delete|forget|shutdown|shut\s+down|restart|reboot|admin|administrator|elevated|powershell|run|execute|"
                         r"script|remember|note|notes|summari[sz]e|explain|why|write|draft|compare)\b", re.I)
_VERB_START = re.compile(r"\s*(?:open|play|launch|start)\b", re.I)


def simple_command(text: str) -> bool:
    """True for a short, single-purpose command the small model handles as well as the big one (measured: volume, brightness,
    media keys, open/play, tasks, system info). Memory, shell, file changes and open-ended questions stay on the big model."""
    words = (text or "").split()
    if not 1 <= len(words) <= 12 or _NOT_SIMPLE.search(text):
        return False
    groups = match(text)
    if groups:
        return len(groups) == 1 and next(iter(groups)) in SIMPLE_GROUPS
    return bool(_VERB_START.match(text))
