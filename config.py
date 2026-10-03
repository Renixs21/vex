"""Loads .env and config.toml into one settings object. Bad values warn and fall back to defaults."""
from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from copy import deepcopy
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("vex.config")

DEFAULTS: dict[str, dict[str, Any]] = {
    "llm": {
        "order": ["groq", "gemini"],     # providers in preference order
        "tool_groups": True,             # send a core tool set + matching groups; false = send every tool every time
        "gemini_model": "",
        "groq_model": "",
        "gemini_rpm": 5,
        "groq_rpm": 25,
        "groq_reasoning_effort": "low",   # gpt-oss models only: low = short thinking, none leaked into replies
        "max_steps": 8,
        "failover_attempts": 2,      # tries per provider on 429/5xx before switching
        "failover_cooldown": 300,    # seconds to stay on the fallback before trying the primary again
    },
    "safety": {"extra_safe_folders": [], "confirm_timeout": 30, "typed_timeout": 60},
    "paths": {"vault": "VexVault", "logs": "logs"},
    "ui": {"hotkey": "windows+alt+v", "caption_seconds": 8, "visual": "pill"},
    "wakeword": {"enabled": True},
}


def _merge(base: dict, over: dict) -> dict:
    """Merge `over` into `base` one level deep, keeping only values of the default's type."""
    for section, values in over.items():
        if section not in base or not isinstance(values, dict):
            base[section] = values
            continue
        for key, val in values.items():
            default = base[section].get(key)
            if default is not None and not isinstance(val, type(default)):
                log.warning("config [%s] %s=%r has the wrong type; using default %r", section, key, val, default)
                continue
            base[section][key] = val
    return base


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return settings: defaults overlaid with config.toml."""
    load_dotenv(ROOT / ".env")
    cfg = deepcopy(DEFAULTS)
    path = path or ROOT / "config.toml"
    if path.exists():
        try:
            cfg = _merge(cfg, tomllib.loads(path.read_text(encoding="utf-8")))
        except (tomllib.TOMLDecodeError, OSError) as e:
            log.warning("could not read %s (%s); using defaults", path, e)
    return cfg


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)       # JSON strings are valid TOML basic strings


def set_toml_value(section: str, key: str, value: Any, path: Path | None = None) -> None:
    """Change one setting in config.toml in place, keeping comments and layout; add the key or section if missing.
    Also updates the in-memory `settings`."""
    path = path or ROOT / "config.toml"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    literal = _toml_literal(value)
    header = re.compile(r"^\s*\[\s*" + re.escape(section) + r"\s*\]\s*(#.*)?$")
    any_header = re.compile(r"^\s*\[")
    key_line = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)([^#]*?)(\s*(?:#.*)?)$")
    start = next((i for i, l in enumerate(lines) if header.match(l)), None)
    if start is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{section}]", f"{key} = {literal}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if any_header.match(lines[i])), len(lines))
        for i in range(start + 1, end):
            if m := key_line.match(lines[i]):
                lines[i] = f"{m.group(1)}{literal}{m.group(3) if m.group(3).strip() else ''}"
                break
        else:
            last = max((i for i in range(start + 1, end) if lines[i].strip()), default=start)
            lines.insert(last + 1, f"{key} = {literal}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    try:
        settings.setdefault(section, {})[key] = value
    except NameError:                                       # called while the module is still loading
        pass


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def dry_run() -> bool:
    return env("DRY_RUN").lower() in ("1", "true", "yes")


def setup_logging(level: int = logging.INFO) -> None:
    """Console (warnings) + rotating file log in logs/."""
    logs = ROOT / settings["paths"]["logs"]
    logs.mkdir(exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fh = RotatingFileHandler(logs / "vex.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)


settings = load()
