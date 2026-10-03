"""Check your microphone and the voice chain, by speaking to it.

    python tools_dev/mic_check.py                 # records until you stop talking, shows levels, what the VAD heard
    python tools_dev/mic_check.py --transcribe    # ...and sends it to Groq Whisper and prints the text
    python tools_dev/mic_check.py --list          # input devices

Nothing is saved: the audio stays in memory.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcribe", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--device", default=None, help="device index or part of its name (default: [voice] input_device)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import numpy as np
    import sounddevice as sd

    import config

    if args.list:
        di = sd.default.device[0]
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and sd.query_hostapis(d["hostapi"])["name"] in ("MME", "Windows WASAPI"):
                print(f"{'*' if i == di else ' '} {i:2d} {d['name'][:60]:60s} {d['max_input_channels']}ch {int(d['default_samplerate'])} Hz")
        return 0

    from voice.recorder import MicUnavailable, Recorder

    cfg = config.settings["voice"]
    device = args.device if args.device is not None else cfg["input_device"]
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    bar = [0.0]

    def level(v: float) -> None:
        bar[0] = v
        print("\r  level " + "#" * int(v * 40) + " " * (40 - int(v * 40)) + f" {v:4.2f}", end="", flush=True)

    rec = Recorder(device=device, silence_ms=cfg["silence_ms"], max_seconds=cfg["max_seconds"], no_speech_seconds=8,
                   vad_aggressiveness=cfg["vad_aggressiveness"], on_level=level)
    print(f"Say something (e.g. 'open notepad'), then stop. VAD: {type(rec.vad).__name__}. Waiting up to 8 s...")
    try:
        out = rec.record()
    except MicUnavailable as e:
        print(f"\nMICROPHONE PROBLEM: {e}\nCheck Settings > Privacy & security > Microphone > 'Let desktop apps access your microphone'.")
        return 1
    print()
    if out.reason == "no_speech":
        print("Nothing heard. The microphone works but no speech was detected: speak closer/louder, or check the input device "
              "and its volume in Windows Sound settings.")
        return 2
    rms = float(np.sqrt(np.mean(out.audio.astype(np.float32) ** 2)))
    print(f"Captured {out.speech_seconds:.1f}s of speech (ended by {out.reason}); loudness RMS {rms:.0f} (speech is usually 300-4000).")
    if args.transcribe:
        from voice.stt import GroqWhisper

        stt = GroqWhisper(api_key=config.env("GROQ_API_KEY"), model=cfg["stt_model"], language=cfg["language"], prompt=cfg["stt_prompt"])
        t = time.monotonic()
        print(f"Whisper heard: {stt.transcribe(out.audio)!r}  ({time.monotonic() - t:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
