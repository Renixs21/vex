"""Vex health check: is every part working, in plain words.

    python tools_dev/health.py              # everything that costs no API quota (the default; also tray > Health check)
    python tools_dev/health.py --live       # also one tiny request per language model and 1 s of speech recognition: asks first

Checks: API keys present, each language model provider (key valid, model available), speech recognition, speech output (edge-tts and
the offline Windows voice), the microphone (1 s of sound), the wake word model, the running Vex (hotkey, wake word, orb, mute, reminders:
from the heartbeat it writes every 30 s), memory (notes, index), reminders, disk space / log sizes / vault size, autostart.

The default run uses NO quota: providers are asked "does this model exist for this key?" (a free lookup), never to generate anything.
Keys are never printed or logged: every line passes through a redactor that hides every value from .env and anything key-shaped.
The report goes to the screen and to logs/health.txt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("vex.health")

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_TTS_API_KEY", "TAVILY_API_KEY")
HEARTBEAT_STALE = 90.0
NEAR_SILENT = 20.0                    # same rule as voice/recorder.py NEAR_SILENT_RMS
VAULT_CAP_MB = 200.0
_KEY_SHAPES = re.compile(r"\b(?:gsk_[A-Za-z0-9]{12,}|AIza[0-9A-Za-z_\-]{20,}|tvly-[A-Za-z0-9\-]{12,}|sk-[A-Za-z0-9\-_]{16,})")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


# ---- secrets never leave this module -------------------------------------------------------------------------------------------------
class Redactor:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        text = str(text)
        for s in self.secrets:
            text = text.replace(s, "[hidden]")
        return _KEY_SHAPES.sub("[hidden]", text)


def secrets_from_env(root: Path) -> list[str]:
    values = [os.environ.get(k, "") for k in os.environ if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS)", k, re.I)]
    try:
        from dotenv import dotenv_values

        values += [v for v in dotenv_values(root / ".env").values() if v]
    except Exception:  # noqa: BLE001
        pass
    return values


def _err(e: BaseException) -> str:
    msg = str(e).splitlines()[0] if str(e) else ""
    return f"{type(e).__name__}: {msg[:160]}" if msg else type(e).__name__


# ---- individual checks (each returns Check / list[Check]; probes are injectable for tests) ---------------------------------------------
def check_keys(env: Callable[[str], str]) -> Check:
    have = [k for k in KEY_NAMES if env(k)]
    need = [k for k in ("GROQ_API_KEY", "GEMINI_API_KEY") if not env(k)]
    detail = "set: " + (", ".join(have) or "none")
    if not have:
        return Check("API keys", FAIL, detail, "Put GROQ_API_KEY and/or GEMINI_API_KEY in .env (see .env.example).")
    if need:
        return Check("API keys", WARN, detail + "; missing: " + ", ".join(need), "Without both, there is no backup brain / speech recognition.")
    return Check("API keys", PASS, detail)


class ModelMissing(Exception):
    status_code = 404


_groq_models: dict[str, set[str]] = {}


def groq_lookup(key: str, model: str) -> None:
    """Free: list the models this key can use (one call per run; model ids contain '/', so a per-model lookup URL doesn't work)."""
    if key not in _groq_models:
        from groq import Groq

        _groq_models[key] = {m.id for m in Groq(api_key=key, timeout=8.0, max_retries=0).models.list().data}
    if model not in _groq_models[key]:
        raise ModelMissing(model)


def gemini_lookup(key: str, model: str) -> None:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=8000))     # keep a reference: a closed client can't send
    client.models.get(model=model)


def groq_tiny(key: str, model: str) -> None:
    from groq import Groq

    Groq(api_key=key, timeout=15.0, max_retries=0).chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Reply with OK."}], max_completion_tokens=16)


def gemini_tiny(key: str, model: str) -> None:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15000))
    client.models.generate_content(model=model, contents="Reply with OK.", config=types.GenerateContentConfig(max_output_tokens=5))


def _status_code(e: BaseException) -> int | None:
    for a in ("status_code", "code", "status"):
        v = getattr(e, a, None)
        if isinstance(v, int):
            return v
    return None


def check_llms(llm_cfg: dict, env: Callable[[str], str], live: bool = False,
               lookup: dict | None = None, tiny: dict | None = None) -> list[Check]:
    lookup = lookup or {"groq": groq_lookup, "gemini": gemini_lookup}
    tiny = tiny or {"groq": groq_tiny, "gemini": gemini_tiny}
    specs = {"groq": ("GROQ_API_KEY", "groq_model"), "groq-small": ("GROQ_API_KEY", "groq_small_model"),
             "gemini": ("GEMINI_API_KEY", "gemini_model")}
    out = []
    for i, name in enumerate(llm_cfg.get("order", [])):
        if name not in specs:
            continue
        key_name, model_key = specs[name]
        model, key = llm_cfg.get(model_key, ""), env(key_name)
        label = f"Brain #{i + 1}: {name} ({model})"
        if not key:
            out.append(Check(label, SKIP, f"no {key_name}", "That provider is skipped in the fallback ladder."))
            continue
        kind = "gemini" if name == "gemini" else "groq"
        t0 = time.monotonic()
        try:
            (tiny if live else lookup)[kind](key, model)
            what = "answered a tiny request" if live else "key valid, model available (no quota used)"
            out.append(Check(label, PASS, f"{what} in {time.monotonic() - t0:.1f} s"))
        except Exception as e:  # noqa: BLE001
            code = _status_code(e)
            if code == 429:
                out.append(Check(label, WARN, "rate-limited right now (429)", "Vex falls back to the next provider; this clears by itself."))
            elif code in (401, 403):
                out.append(Check(label, FAIL, f"the key was refused ({code})", f"Check {key_name} in .env."))
            elif code == 404:
                out.append(Check(label, FAIL, "this model name doesn't exist (404)", f"Fix [llm] {model_key} in config.toml."))
            else:
                out.append(Check(label, FAIL, f"couldn't reach it: {_err(e)}", "Offline, or the provider is down; Vex uses the next one."))
    if not out:
        out.append(Check("Brain", FAIL, "no language model provider configured", "Set [llm] order and the keys."))
    return out


def check_stt(voice_cfg: dict, env: Callable[[str], str], live: bool = False, lookup: Callable | None = None,
              transcribe: Callable | None = None) -> Check:
    key, model = env("GROQ_API_KEY"), voice_cfg.get("stt_model", "whisper-large-v3-turbo")
    backup = " Backup: Gemini (checked above)." if voice_cfg.get("stt_fallback", "gemini") == "gemini" else " No backup configured."
    if not key:
        return Check("Speech recognition", FAIL if not env("GEMINI_API_KEY") else WARN, "no GROQ_API_KEY: Groq Whisper is off." + backup)
    t0 = time.monotonic()
    try:
        if live:
            import numpy as np

            (transcribe or _whisper_tiny)(key, model, (np.random.default_rng(0).standard_normal(16000) * 30).astype(np.int16))
            what = "transcribed 1 s of test audio"
        else:
            (lookup or groq_lookup)(key, model)
            what = "Whisper model available (no quota used)"
        return Check(f"Speech recognition (Groq {model})", PASS, f"{what} in {time.monotonic() - t0:.1f} s.{backup}")
    except Exception as e:  # noqa: BLE001
        code = _status_code(e)
        hint = "Check GROQ_API_KEY." if code in (401, 403) else "Vex falls back to the backup, then to typing."
        return Check(f"Speech recognition (Groq {model})", WARN if code == 429 else FAIL, f"{code or ''} {_err(e)}".strip() + "." + backup, hint)


def _whisper_tiny(key: str, model: str, audio) -> None:
    from voice.stt import GroqWhisper

    GroqWhisper(api_key=key, model=model, timeout=10.0).transcribe(audio)


def check_tts(edge_voice: str, synth: Callable[[str], object] | None = None, windows_ok: Callable[[], bool] | None = None) -> list[Check]:
    out = []
    t0 = time.monotonic()
    try:
        if synth is None:
            from voice.tts import EdgeTTS

            synth = EdgeTTS(edge_voice).synthesize
        synth("OK.")
        out.append(Check(f"Speech output (edge-tts, {edge_voice})", PASS, f"spoke a test word in {time.monotonic() - t0:.1f} s (free, no key)"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(f"Speech output (edge-tts, {edge_voice})", WARN, _err(e), "Offline? Vex uses the Windows voice instead."))
    try:
        ok = windows_ok() if windows_ok else __import__("sys").platform == "win32"
        out.append(Check("Offline Windows voice", PASS if ok else FAIL, "available" if ok else "not available"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("Offline Windows voice", FAIL, _err(e)))
    return out


def record_one_second(device=None):
    import numpy as np
    import sounddevice as sd

    audio = sd.rec(16000, samplerate=16000, channels=1, dtype="int16", device=device)
    sd.wait()
    return np.asarray(audio).reshape(-1)


def check_mic(record: Callable[[], object] | None = None, windows_muted: Callable[[], bool] | None = None) -> Check:
    import numpy as np

    try:
        audio = np.asarray((record or record_one_second)()).astype(np.float32)
    except Exception as e:  # noqa: BLE001
        return Check("Microphone", FAIL, f"couldn't open it: {_err(e)}", "Settings > Privacy > Microphone: allow desktop apps; check the device.")
    frames = [audio[i:i + 480] for i in range(0, len(audio) - 479, 480)]                 # 30 ms frames, like the recorder
    rms = max((float(np.sqrt(np.mean(f ** 2))) for f in frames), default=0.0)        # the loudest frame, like Vex's own "can't hear you" rule
    try:
        muted = bool((windows_muted or _windows_muted)())
    except Exception:  # noqa: BLE001
        muted = False
    if muted:
        return Check("Microphone", FAIL, "Windows has the default microphone MUTED", "Unmute it in Settings > System > Sound > Input.")
    if audio.size == 0:
        return Check("Microphone", FAIL, "no audio arrived")
    if not np.any(audio):
        return Check("Microphone", FAIL, "it delivers pure digital silence (all zeros)",
                     "A hardware mute switch, a disabled device, or the wrong default input in Windows Sound settings.")
    if rms < NEAR_SILENT:
        return Check("Microphone", PASS, f"working; quiet room (loudest sound RMS {rms:.1f} in 1 s; speak during the check to test your level)")
    return Check("Microphone", PASS, f"1 s recorded, loudest sound RMS {rms:.0f}")


def _windows_muted() -> bool:
    from voice.micstate import default_mic_muted

    return bool(default_mic_muted())


def check_wakeword(wcfg: dict, root: Path, load: Callable[[str], object] | None = None) -> Check:
    if not wcfg.get("enabled", True):
        return Check("Wake word", SKIP, "switched off (tray > Wake word)")
    path = root / wcfg.get("model_path", "")
    model = str(path) if path.is_file() else wcfg.get("fallback_model", "hey_jarvis")
    try:
        if load is None:
            from voice.wakeword import _default_model_factory

            load = lambda m: _default_model_factory(m, float(wcfg.get("vad_threshold", 0.0)))  # noqa: E731
        load(model)
    except Exception as e:  # noqa: BLE001
        return Check("Wake word", FAIL, f"the model didn't load: {_err(e)}", "The hotkey still works. See docs/WAKEWORD.md.")
    if not path.is_file():
        return Check("Wake word", WARN, f"{path.name} not found; using the fallback '{model}'", "Train / copy models/hey_vex.onnx.")
    return Check("Wake word", PASS, f"'{path.name}' loads (threshold {wcfg.get('threshold')})")


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_running(status: dict | None, now: float | None = None, pid_alive: Callable[[int], bool] | None = None) -> list[Check]:
    """From the heartbeat the app writes every 30 s (or the live app's own status when run from the tray)."""
    now = time.time() if now is None else now
    if not status:
        return [Check("Vex app", WARN, "not running (no heartbeat)", "Start it: python main.py (or log in again for autostart).")]
    age = now - float(status.get("ts", 0))
    alive = (pid_alive or _pid_alive)(int(status.get("pid", 0)))
    if age > HEARTBEAT_STALE or not alive:
        return [Check("Vex app", WARN, f"not running (last heartbeat {age / 60:.0f} min ago)", "Start it: python main.py")]
    out = [Check("Vex app", PASS, f"running (pid {status.get('pid')}, {status.get('state', '?')})")]
    out.append(Check("Hotkey", PASS if status.get("hotkey") else FAIL,
                     "registered" if status.get("hotkey") else "not registered", "" if status.get("hotkey") else "Another app may own the combo."))
    wake = status.get("wakeword", "off")
    if status.get("mic_muted"):
        out.append(Check("Listening", WARN, "the microphone is muted in Vex (tray > Mute mic / pause listening)"))
    elif wake == "running":
        out.append(Check("Listening", PASS, "wake word listening" + (", follow-up on" if status.get("followup") else "")))
    elif wake == "off":
        out.append(Check("Listening", WARN, "wake word switched off: hotkey only"))
    else:
        out.append(Check("Listening", FAIL, f"wake word {wake}", "Vex retries by itself; see logs/vex.log."))
    orb = status.get("orb", "?")
    out.append(Check("Orb", PASS if orb in ("running", "external") else (WARN if orb == "pill" else FAIL), {
        "running": "running", "external": "running (started outside Vex)", "pill": "using the built-in pill (orb off or gave up)"}.get(orb, orb)))
    out.append(Check("Reminder scheduler", PASS if status.get("reminders") else FAIL, "running" if status.get("reminders") else "not running"))
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return True


def check_memory(vault: Path) -> Check:
    if not vault.is_dir():
        return Check("Memory", FAIL, f"vault folder missing: {vault}")
    notes = [p for p in vault.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(vault).parts)]
    db = vault / ".vexmind" / "index.db"
    if not db.exists():
        cands = list(vault.rglob("index.db"))
        db = cands[0] if cands else db
    if not db.exists():
        return Check("Memory", WARN, f"{len(notes)} notes; no search index yet", "It is rebuilt automatically on the next start.")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            ok = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        return Check("Memory", FAIL, f"{len(notes)} notes; index can't be read: {_err(e)}", "Delete the index file; Vex rebuilds it.")
    if ok != "ok":
        return Check("Memory", FAIL, f"{len(notes)} notes; index damaged ({ok})", "Delete the index file; Vex rebuilds it.")
    return Check("Memory", PASS, f"{len(notes)} notes, index OK")


def check_reminders(path: Path) -> Check:
    if not path.exists():
        return Check("Reminders", PASS, "none saved yet")
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check("Reminders", FAIL, f"the reminders file can't be read: {_err(e)}", f"Look at {path}.")
    pending = [i for i in items if not i.get("done")]
    return Check("Reminders", PASS, f"{len(pending)} pending, {len(items) - len(pending)} done")


def folder_mb(path: Path) -> float:
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total / 1_000_000


def check_disk(root: Path, logs: Path, vault: Path, free_bytes: Callable[[Path], int] | None = None,
               size_mb: Callable[[Path], float] = folder_mb, cap_mb: float = VAULT_CAP_MB) -> list[Check]:
    free = (free_bytes or (lambda p: shutil.disk_usage(p).free))(root) / 1e9
    out = [Check("Disk space", FAIL if free < 0.5 else WARN if free < 2 else PASS, f"{free:.1f} GB free on {root.anchor or root}",
                 "Free some space: Vex needs room for logs and memory." if free < 2 else "")]
    lmb = size_mb(logs)
    out.append(Check("Logs", WARN if lmb > 100 else PASS, f"{lmb:.1f} MB in {logs.name}", "Housekeeping trims them." if lmb > 100 else ""))
    vmb = size_mb(vault)
    pct = vmb / cap_mb * 100
    out.append(Check("Vault size", WARN if pct >= 80 else PASS, f"{vmb:.1f} MB of the {cap_mb:.0f} MB soft cap ({pct:.0f}%)",
                     "Near the cap: automatic memory entries pause at 100%; your own notes are never deleted." if pct >= 80 else ""))
    return out


def check_autostart() -> Check:
    try:
        from ui import autostart

        on = autostart.is_enabled()
        return Check("Start with Windows", PASS, "on" if on else f"off (your choice: {autostart.read_state() or 'not decided'})")
    except Exception as e:  # noqa: BLE001
        return Check("Start with Windows", WARN, _err(e))


# ---- running it all -----------------------------------------------------------------------------------------------------------------------
def run_checks(live: bool = False, status: dict | None = None, probes: dict | None = None) -> list[Check]:
    """Every check, in order. `status`: the live app's status (from the tray); otherwise the heartbeat file. `probes` override pieces in tests."""
    import config

    p = probes or {}
    env = p.get("env", config.env)
    root = config.ROOT
    logs = root / config.settings["paths"]["logs"]
    vault = root / config.settings["paths"]["vault"]
    steps: list[tuple[str, Callable[[], object]]] = [
        ("keys", lambda: check_keys(env)),
        ("llm", lambda: check_llms(config.settings["llm"], env, live, p.get("llm_lookup"), p.get("llm_tiny"))),
        ("stt", lambda: check_stt(config.settings["voice"], env, live, p.get("stt_lookup"), p.get("stt_transcribe"))),
        ("tts", lambda: check_tts(config.settings["voice"].get("edge_voice", "en-US-AriaNeural"), p.get("tts_synth"), p.get("windows_tts"))),
        ("mic", lambda: check_mic(p.get("record"), p.get("windows_muted"))),
        ("wake", lambda: check_wakeword(config.wakeword_config(), root, p.get("wake_load"))),
        ("app", lambda: check_running(status if status is not None else read_heartbeat(root / "data" / "status.json"),
                                      pid_alive=p.get("pid_alive"))),
        ("memory", lambda: check_memory(vault)),
        ("reminders", lambda: check_reminders(root / "data" / "reminders.json")),
        ("disk", lambda: check_disk(root, logs, vault, p.get("free_bytes"), p.get("size_mb", folder_mb))),
        ("autostart", check_autostart),
    ]
    out: list[Check] = []
    for name, fn in steps:
        try:
            r = fn()
            out.extend(r if isinstance(r, list) else [r])
        except Exception as e:  # noqa: BLE001 - a broken check is itself a finding, never a crash
            out.append(Check(name, FAIL, f"the check itself failed: {_err(e)}"))
    return out


def render(checks: list[Check], live: bool, redact: Callable[[str], str]) -> str:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    lines = [f"Vex health check, {datetime.now():%Y-%m-%d %H:%M}"
             + ("  (live: tiny requests were made)" if live else "  (no API quota used)"), ""]
    width = max((len(c.name) for c in checks), default=10)
    for c in checks:
        lines.append(f"{c.status:4}  {c.name:<{width}}  {c.detail}")
        if c.hint and c.status in (WARN, FAIL):
            lines.append(f"{'':4}  {'':<{width}}  -> {c.hint}")
    verdict = "All good." if not counts[FAIL] and not counts[WARN] else (
        "Working, with warnings." if not counts[FAIL] else "Something needs fixing (see FAIL above).")
    lines += ["", f"{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed, {counts[SKIP]} skipped. {verdict}"]
    return redact("\n".join(lines))


def write_report(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def summary_line(checks: list[Check]) -> str:
    f = [c.name for c in checks if c.status == FAIL]
    w = [c.name for c in checks if c.status == WARN]
    if f:
        return f"{len(f)} problem(s): {', '.join(f[:3])}" + ("…" if len(f) > 3 else "")
    if w:
        return f"Working, {len(w)} warning(s): {', '.join(w[:3])}" + ("…" if len(w) > 3 else "")
    return "Everything passed."


# ---- the heartbeat the app writes ------------------------------------------------------------------------------------------------------------
def write_heartbeat(path: Path, status: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "pid": os.getpid(), **status}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.debug("could not write the heartbeat", exc_info=True)


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python tools_dev/health.py", description="Check that every part of Vex works.")
    ap.add_argument("--live", action="store_true", help="also make one tiny request per model and 1 s of speech recognition (asks first)")
    ap.add_argument("--yes", action="store_true", help="with --live: don't ask")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    import config

    live = a.live
    if live and not a.yes:
        q = ("--live sends one tiny request to each language model (about 20-60 tokens each, out of your daily free allowance) and 1 s of "
             "audio to Groq Whisper (billed as up to 10 s of the 7200 s/hour). Go ahead? [y/N] ")
        if ask(q).strip().lower() not in ("y", "yes"):
            print("OK, running the free checks only.")
            live = False
    checks = run_checks(live=live)
    text = render(checks, live, Redactor(secrets_from_env(config.ROOT)))
    print(text)
    path = write_report(text, config.ROOT / config.settings["paths"]["logs"] / "health.txt")
    print(f"\n(saved to {path})")
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
