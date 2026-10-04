"""Hear Vex's candidate voices and pick one.

    python tools_dev/tts_voices.py                       # speaks one sentence in each candidate Chirp 3 HD voice
    python tools_dev/tts_voices.py --voices Kore Puck    # just these (short names, or full names like en-US-Chirp3-HD-Kore)
    python tools_dev/tts_voices.py --edge                # also the edge-tts and Windows fallback voices
    python tools_dev/tts_voices.py --list                # every Chirp 3 HD voice Google offers for your language
    python tools_dev/tts_voices.py --usage               # characters used this month and what is left
    python tools_dev/tts_voices.py --set Kore            # save the choice to config.toml ([voice] google_voice)

The candidates come from [voice] google_candidates in config.toml. Each sentence is about 60 characters (the free tier is 1,000,000 a
month), and it counts toward the monthly total like any spoken reply. The key is read from GOOGLE_TTS_API_KEY (.env) or
google-tts-key.json and never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SENTENCE = "Hi, I'm {name}. This is how I sound when I answer you."


def full_name(name: str, language: str) -> str:
    return name if "-" in name else f"{language}-Chirp3-HD-{name}"


def short_name(name: str) -> str:
    return name.rsplit("-", 1)[-1]


def build_google(cfg: dict, root: Path, voice: str):
    import config
    from voice.tts import GoogleTTS

    key = root / config.env("GOOGLE_APPLICATION_CREDENTIALS", "google-tts-key.json")
    return GoogleTTS(voice, cfg["google_language"], credentials=key, api_key=config.env("GOOGLE_TTS_API_KEY"))


def speak(audio) -> None:
    import sounddevice as sd

    sd.play(audio.samples, audio.rate)
    sd.wait()


def usage_line(chain) -> str:
    s = chain.status()
    return (f"Google voice this month: {s['used']:,} of {s['limit']:,} free characters used ({s['fraction']:.1%}); "
            f"{s['limit'] - s['used']:,} left.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices", nargs="*", help="voices to try (default: [voice] google_candidates)")
    ap.add_argument("--edge", action="store_true", help="also speak the edge-tts and Windows fallback voices")
    ap.add_argument("--list", action="store_true", help="list the Chirp 3 HD voices Google offers")
    ap.add_argument("--usage", action="store_true", help="show this month's character count and exit")
    ap.add_argument("--set", metavar="NAME", help="save this voice as [voice] google_voice in config.toml")
    ap.add_argument("--text", default=SENTENCE, help="sentence to speak; {name} is replaced by the voice name")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    import config
    from voice.tts import EdgeTTS, TTSChain, TTSError, WindowsTTS
    from voice.usage import UsageStore

    cfg = config.settings["voice"]
    root = config.ROOT
    usage = UsageStore(root / "data" / "voice_usage.json")
    lang = cfg["google_language"]
    probe = build_google(cfg, root, cfg["google_voice"])
    chain = TTSChain([probe], usage, monthly_limit=cfg["tts_monthly_free_chars"], switch_fraction=cfg["tts_switch_fraction"],
                     notify=lambda title, msg: print(f"  ! {title}: {msg}"))

    if args.usage:
        print(usage_line(chain))
        print(f"Key: {'set (' + probe.mode + ')' if probe.available() else 'NOT set: add GOOGLE_TTS_API_KEY to .env'}")
        return 0
    if not probe.available():
        print("No Google key found. Put GOOGLE_TTS_API_KEY=... in .env (or google-tts-key.json in the Vex folder).\n"
              "Steps: docs/VOICE.md, section 'Google Chirp 3 HD'.")
        if not args.edge:
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
        if probe.available():
            try:
                names = probe.voice_names()
            except TTSError as e:
                print("Couldn't check the voice name with Google:", e, "(saving anyway)")
            else:
                if voice not in names:
                    print(f"{voice} is not a voice Google offers for {lang}. Try --list.")
                    return 1
        config.set_toml_value("voice", "google_voice", voice)
        print(f"Saved: [voice] google_voice = \"{voice}\" in config.toml. Restart Vex to use it.")
        return 0

    candidates = [full_name(v, lang) for v in (args.voices or cfg["google_candidates"])]
    if probe.available():
        print(f"Speaking one sentence in each of {len(candidates)} voices. Current choice: {short_name(cfg['google_voice'])}.\n")
        for voice in candidates:
            name = short_name(voice)
            text = args.text.format(name=name)
            g = build_google(cfg, root, voice)
            one = TTSChain([g], usage, monthly_limit=cfg["tts_monthly_free_chars"], switch_fraction=1.0,
                           notify=lambda title, msg: print(f"  ! {title}: {msg}"))
            print(f"  {name:<14}", end="", flush=True)
            try:
                speak(one.synthesize(text))
                print(f"ok ({len(text)} characters)")
            except Exception as e:  # noqa: BLE001
                print(f"failed: {e}")
                if "rejected" in str(e) or "quota" in str(e).lower():
                    break
    if args.edge:
        for label, provider in (("edge-tts", EdgeTTS(cfg["edge_voice"])), ("Windows (offline)", WindowsTTS(cfg.get("windows_voice", "")))):
            print(f"  {label:<18}", end="", flush=True)
            try:
                speak(provider.synthesize(args.text.format(name=label)))
                print("ok")
            except Exception as e:  # noqa: BLE001
                print(f"failed: {e}")
    print("\n" + usage_line(chain))
    print("Pick one with:  python tools_dev/tts_voices.py --set <name>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
