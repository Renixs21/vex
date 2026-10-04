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
        "order": ["groq", "groq-small", "gemini"],     # providers in preference order
        "tool_groups": True,             # send a core tool set + matching groups; false = send every tool every time
        "gemini_model": "",
        "groq_model": "",
        "groq_small_model": "",          # a smaller, faster Groq model: next fallback, and first choice for simple commands
        "route_simple": True,            # send simple one-tool commands to groq-small first
        "gemini_rpm": 5,
        "groq_rpm": 25,
        "groq_reasoning_effort": "low",   # gpt-oss models only: low = short thinking, none leaked into replies
        "max_steps": 8,
        "failover_attempts": 2,      # tries per provider on 429/5xx before switching
        "failover_cooldown": 300,    # seconds to stay on the fallback before trying the primary again
    },
    "safety": {"extra_safe_folders": [], "confirm_timeout": 30, "typed_timeout": 60},
    "paths": {"vault": "VexVault", "logs": "logs"},
    "ui": {
        "hotkey": "ctrl+alt+v", "caption_seconds": 8,
        "visual": "orb",                 # orb (the marbled orb app), pill (small built-in Qt widget) or both
        "orb_dir": "vex_ui", "orb_position": "bottom", "orb_palette": "blue", "orb_size": "", "orb_autosleep": 20,
        "orb_mode": "dock", "orb_edge": "right", "orb_edge_y": 0.40, "orb_center_y": 0.50, "orb_radius": 62.0,
        "orb_port": 47474, "orb_click_port": 47475,
    },
    "memory": {
        "curator": True,                 # after a conversation, save durable facts from it (see memory/curator.py)
        "curator_idle_seconds": 120,     # a conversation is over after this much quiet
        "curator_order": ["groq-small", "groq", "gemini"],   # cheap model first: it must not use up what live replies need
        "curator_min_words": 6,          # conversations with fewer user words in total are skipped without an LLM call
        "curator_max_attempts": 6,       # then the conversation is dropped (and logged)
        "consolidate": True,             # nightly tidy-up (~03:00, or at the next start if missed); log: logs/memory_consolidation.jsonl
    },
    "wakeword": {"enabled": True, "model_path": "models/hey_vex.onnx", "fallback_model": "hey_jarvis", "threshold": 0.5,
                 "cooldown": 2.0, "vad_threshold": 0.5, "input_device": "", "chime": True},
    "voice": {
        "enabled": True,
        "input_device": "",              # blank = Windows default microphone; or part of a device name
        "output_device": "",
        "silence_ms": 1000,              # this much quiet after you speak ends the recording
        "max_seconds": 20,
        "no_speech_seconds": 6,          # give up if nothing is said for this long
        "vad_aggressiveness": 2,         # 0-3, higher filters more noise
        "preroll_ms": 500,
        "stt_model": "whisper-large-v3-turbo",
        "language": "en",                # "" = let Whisper detect the language (Hindi, Kannada...)
        "stt_prompt": "Hey Vex. Vex, open Notepad. Hey Vex, set the volume.",
        "stt_seconds_per_hour": 7200,    # Groq's free Whisper allowance; checked locally so we fail to typing early
        "tts_order": ["edge", "windows"],   # edge-tts, then the offline Windows voice: never silent. Add "google" first to use Chirp (docs/VOICE.md)
        "edge_candidates": ["en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural", "en-IN-PrabhatNeural", "en-US-AriaNeural", "en-US-AvaNeural",
                           "en-US-AvaMultilingualNeural", "en-US-EmmaNeural", "en-US-JennyNeural", "en-US-MichelleNeural", "en-US-AndrewNeural",
                           "en-US-BrianNeural", "en-US-ChristopherNeural", "en-US-GuyNeural", "en-GB-SoniaNeural", "en-GB-LibbyNeural",
                           "en-GB-RyanNeural"],   # tools_dev/tts_voices.py speaks one sentence in each
        "google_candidates": ["Aoede", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Zephyr"],   # tools_dev/tts_voices.py
        "tts_warn_fractions": [0.75, 0.9],   # tray warnings at these shares of the free allowance
        "tts_quota_pause_seconds": 3600,     # after a quota/billing error, skip Google for this long
        "windows_voice": "",                 # blank = the Windows default voice
        "google_voice": "en-US-Chirp3-HD-Aoede",
        "google_language": "en-US",
        "edge_voice": "en-US-AriaNeural",
        "tts_monthly_free_chars": 1000000,   # Google's free monthly characters for Chirp 3 HD (check your console)
        "tts_switch_fraction": 0.9,          # switch to edge-tts at this share of the free allowance
        "spoken_max_chars": 280,
        "spoken_sentences": 2,           # longer replies: speak this many sentences, show the rest ("read the rest" speaks it)
        "confirm_by_voice": True,        # yellow confirmations can be answered by voice (red never)
        "confirm_voice_seconds": 6,
    },
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


def wakeword_config() -> dict:
    """The [wakeword] section with every value checked: a bad value is logged and replaced by its default, never raised."""
    d = dict(DEFAULTS["wakeword"])
    raw = settings.get("wakeword", {}) or {}
    log = logging.getLogger("vex.config")
    ranges = {"threshold": (0.05, 0.99), "cooldown": (0.0, 60.0), "vad_threshold": (0.0, 0.99)}
    for key, default in DEFAULTS["wakeword"].items():
        if key not in raw:
            continue
        v = raw[key]
        if isinstance(default, bool):
            ok = isinstance(v, bool)
        elif key in ranges:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and ranges[key][0] <= float(v) <= ranges[key][1]
        else:
            ok = isinstance(v, str)
        if ok:
            d[key] = float(v) if key in ranges else v
        else:
            log.warning("[wakeword] %s = %r is not valid; using %r", key, v, default)
    return d


def log_file() -> Path:
    return ROOT / settings["paths"]["logs"] / "vex.log"


def setup_logging(level: int = logging.INFO, console: bool = False) -> None:
    """Rotating file log in logs/vex.log, plus the terminal: warnings only, or everything at INFO when console=True."""
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
    ch.setLevel(logging.INFO if console else logging.WARNING)
    if console:
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(ch)


settings = load()
