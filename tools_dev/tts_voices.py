"""Hear Vex's candidate voices and pick one.

    python tools_dev/tts_voices.py                       # speaks one sentence in each candidate edge-tts voice (free, no key)
    python tools_dev/tts_voices.py --voices Neerja Ava   # just these (full names like en-IN-NeerjaNeural, or part of a name)
    python tools_dev/tts_voices.py --list                # every English edge-tts voice (--locale en-IN to narrow it)
    python tools_dev/tts_voices.py --set en-IN-NeerjaNeural   # save the choice to config.toml ([voice] edge_voice)
    python tools_dev/tts_voices.py --windows             # also the offline Windows voice
    python tools_dev/tts_voices.py --google              # the same for Google Chirp voices (needs a key; optional, see docs/VOICE.md)

The candidates come from [voice] edge_candidates in config.toml. Nothing is installed or paid for: edge-tts is free and needs only
internet. With --google the key is read from GOOGLE_TTS_API_KEY (.env) or google-tts-key.json and never printed, and each sentence counts
toward the monthly character allowance (--usage shows it).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SENTENCE = "Hi, I'm {name}. This is how I sound when I answer you."


# ---- naming helpers (shared by the edge and Google modes) ------------------------------------------------------------------------
def full_name(name: str, language: str) -> str:
    """Google: 'Kore' -> 'en-US-Chirp3-HD-Kore'."""
    return name if "-" in name else f"{language}-Chirp3-HD-{name}"


def short_name(name: str) -> str:
    """'en-US-Chirp3-HD-Kore' -> 'Kore'; 'en-IN-NeerjaNeural' -> 'Neerja'."""
    last = name.rsplit("-", 1)[-1]
    return last[:-6] if last.endswith("Neural") and len(last) > 6 else last


def resolve_edge(name: str, candidates: list[str], available: list[str] | None = None) -> str | None:
    """A full edge voice name from what the user typed: exact, or a unique-ish part ('Neerja', 'ava', 'en-GB-Sonia')."""
    pools = [candidates] + ([available] if available else [])
    n = name.lower()
    for pool in pools:
        for v in pool:
            if v.lower() == n:
                return v
    for pool in pools:
        hits = [v for v in pool if n in v.lower()]
        if hits:
            return sorted(hits, key=len)[0]                      # 'ava' -> en-US-AvaNeural before AvaMultilingualNeural
    return None


async def _edge_voices() -> list[dict]:
    import edge_tts

    return await edge_tts.list_voices()


def edge_voices(locale: str = "en-") -> list[dict]:
    """English (or one locale's) voices edge-tts offers right now. Needs internet."""
    return [v for v in asyncio.run(asyncio.wait_for(_edge_voices(), timeout=20)) if v["Locale"].startswith(locale)]


def speak(audio) -> None:
    import sounddevice as sd

    sd.play(audio.samples, audio.rate)
    sd.wait()


def build_google(cfg: dict, root: Path, voice: str):
    import config
    from voice.tts import GoogleTTS

    key = root / config.env("GOOGLE_APPLICATION_CREDENTIALS", "google-tts-key.json")
    return GoogleTTS(voice, cfg["google_language"], credentials=key, api_key=config.env("GOOGLE_TTS_API_KEY"))


def usage_line(chain) -> str:
    s = chain.status()
    return (f"Google voice this month: {s['used']:,} of {s['limit']:,} free characters used ({s['fraction']:.1%}); "
            f"{s['limit'] - s['used']:,} left.")


# ---- edge mode (the default) ----------------------------------------------------------------------------------------------------
def run_edge(args, cfg: dict) -> int:
    import config
    from voice.tts import EdgeTTS, TTSError, WindowsTTS

    if args.list:
        try:
            voices = edge_voices(args.locale or "en-")
        except Exception as e:  # noqa: BLE001
            print("Couldn't get the voice list (internet needed):", e)
            return 1
        print(f"{len(voices)} edge-tts voices:")
        for v in sorted(voices, key=lambda v: (v["Locale"], v["ShortName"])):
            tags = ",".join(v.get("VoiceTag", {}).get("VoicePersonalities", []))
            print(f"  {v['ShortName']:<34}{v['Gender']:<8}{tags}")
        return 0

    candidates = list(cfg["edge_candidates"])
    if args.set:
        voice = resolve_edge(args.set, candidates)
        if voice is None:
            try:
                voice = resolve_edge(args.set, candidates, [v["ShortName"] for v in edge_voices(args.locale or "")])
            except Exception as e:  # noqa: BLE001
                print("Couldn't check the voice name (internet needed):", e)
                return 1
        if voice is None:
            print(f"No edge-tts voice matches {args.set!r}. Try --list.")
            return 1
        config.set_toml_value("voice", "edge_voice", voice)
        print(f"Saved: [voice] edge_voice = \"{voice}\" in config.toml. Restart Vex to use it.")
        return 0

    chosen: list[str] = []
    if args.voices:
        for name in args.voices:
            v = resolve_edge(name, candidates)
            if v is None:
                print(f"  (no candidate matches {name!r}; see --list)")
            else:
                chosen.append(v)
    else:
        chosen = candidates
    print(f"Speaking one sentence in each of {len(chosen)} voices. Current choice: {cfg['edge_voice']}.\n")
    for voice in chosen:
        name = short_name(voice)
        mark = "  <- current" if voice == cfg["edge_voice"] else ""
        print(f"  {voice:<34}", end="", flush=True)
        try:
            speak(EdgeTTS(voice).synthesize(args.text.format(name=name)))
            print("ok" + mark)
        except TTSError as e:
            print(f"failed: {e}")
    if args.windows:
        print(f"  {'Windows (offline)':<34}", end="", flush=True)
        try:
            speak(WindowsTTS(cfg.get("windows_voice", "")).synthesize(args.text.format(name="Windows")))
            print("ok")
        except TTSError as e:
            print(f"failed: {e}")
    print("\nPick one with:  python tools_dev/tts_voices.py --set <name>   (for example --set Neerja)")
    return 0


# ---- Google mode (optional) -----------------------------------------------------------------------------------------------------
def run_google(args, cfg: dict) -> int:
    import config
    from voice.tts import TTSChain, TTSError
    from voice.usage import UsageStore

    root = config.ROOT
    usage = UsageStore(root / "data" / "voice_usage.json")
    lang = cfg["google_language"]
    probe = build_google(cfg, root, cfg["google_voice"])
    notify = lambda title, msg: print(f"  ! {title}: {msg}")  # noqa: E731
    chain = TTSChain([probe], usage, monthly_limit=cfg["tts_monthly_free_chars"], switch_fraction=cfg["tts_switch_fraction"], notify=notify)

    if args.usage:
        print(usage_line(chain))
        print(f"Key: {'set (' + probe.mode + ')' if probe.available() else 'NOT set: add GOOGLE_TTS_API_KEY to .env'}")
        return 0
    if not probe.available():
        print("No Google key found. Google voices are optional (Vex works with edge-tts); see docs/VOICE.md, 'Google Chirp (optional)'.")
        return 1
    if args.list:
        try:
            names = sorted(n for n in probe.voice_names() if "Chirp3-HD" in n)
        except TTSError as e:
            print("Couldn't list the voices:", e)
            return 1
        print(f"{len(names)} Chirp 3 HD voices for {lang}:\n  " + ", ".join(short_name(n) for n in names))
        return 0
    if args.set:
        voice = full_name(args.set, lang)
        try:
            names = probe.voice_names()
        except TTSError as e:
            print("Couldn't check the voice name with Google:", e, "(saving anyway)")
        else:
            if voice not in names:
                print(f"{voice} is not a voice Google offers for {lang}. Try --google --list.")
                return 1
        config.set_toml_value("voice", "google_voice", voice)
        print(f"Saved: [voice] google_voice = \"{voice}\" in config.toml. Restart Vex to use it.")
        return 0
    candidates = [full_name(v, lang) for v in (args.voices or cfg["google_candidates"])]
    print(f"Speaking one sentence in each of {len(candidates)} Google voices. Current choice: {short_name(cfg['google_voice'])}.\n")
    for voice in candidates:
        text = args.text.format(name=short_name(voice))
        one = TTSChain([build_google(cfg, root, voice)], usage, monthly_limit=cfg["tts_monthly_free_chars"], switch_fraction=1.0,
                       notify=notify)
        print(f"  {short_name(voice):<14}", end="", flush=True)
        try:
            speak(one.synthesize(text))
            print(f"ok ({len(text)} characters)")
        except Exception as e:  # noqa: BLE001
            print(f"failed: {e}")
            if "rejected" in str(e) or "quota" in str(e).lower():
                break
    print("\n" + usage_line(chain))
    print("Pick one with:  python tools_dev/tts_voices.py --google --set <name>")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices", nargs="*", help="voices to try (default: [voice] edge_candidates)")
    ap.add_argument("--list", action="store_true", help="list the voices on offer")
    ap.add_argument("--locale", default="", help="with --list/--set: only this locale prefix, e.g. en-IN")
    ap.add_argument("--set", metavar="NAME", help="save this voice in config.toml")
    ap.add_argument("--windows", action="store_true", help="also speak the offline Windows voice")
    ap.add_argument("--google", action="store_true", help="work with Google Chirp voices instead (optional, needs a key)")
    ap.add_argument("--usage", action="store_true", help="with --google: this month's character count")
    ap.add_argument("--text", default=SENTENCE, help="sentence to speak; {name} is replaced by the voice name")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    import config

    cfg = config.settings["voice"]
    return run_google(args, cfg) if (args.google or args.usage) else run_edge(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
