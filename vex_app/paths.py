"""Where things are. The Control Center reads two files from the Vex folder (the API token and config.toml's [api] port) and
otherwise talks only to the local API: it never imports Vex's own modules."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent                      # the Vex folder (vex_app/ lives inside it)
QML = APP / "qml"
SHADERS = APP / "shaders"
FONTS = APP / "fonts"
ICONS = APP / "icons"
WEB = APP / "web"
TOKEN = ROOT / "data" / "api_token"
DEFAULT_PORT = 47480


def api_port(config_path: Path | None = None) -> int:
    try:
        cfg = tomllib.loads((config_path or ROOT / "config.toml").read_text(encoding="utf-8"))
        return int(cfg.get("api", {}).get("port", DEFAULT_PORT))
    except (OSError, ValueError, TypeError):
        return DEFAULT_PORT


def read_token(path: Path | None = None) -> str:
    """The API token, or "" if Vex hasn't created it yet. Never logged."""
    try:
        return (path or TOKEN).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def vex_command() -> list[str]:
    """How to start Vex (the "Start it" button): the supervisor, quietly, without a console window."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), str(ROOT / "main.py"), "--supervise", "--quiet"]
