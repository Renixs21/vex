"""Tool modules. Call load_all() once to register every tool (kept explicit to avoid import cycles with safety/)."""
import importlib

MODULES = ("files", "apps", "media", "tasks", "reminders", "web", "system", "shell", "memory", "categories")


def load_all() -> None:
    """Import every tool module so its @tool decorators run."""
    for name in MODULES:
        importlib.import_module(f"tools.{name}")
    importlib.import_module("tools.categories").apply()          # stamp every tool with its group
