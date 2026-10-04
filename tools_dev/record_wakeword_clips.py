"""Record your own voice for training / testing the "Hey Vex" wake word model.

    python tools_dev/record_wakeword_clips.py --set train                 # everything for training (50 pos + 240+ neg)
    python tools_dev/record_wakeword_clips.py --set heldout               # 15 pos + 60 neg for testing the trained model
    python tools_dev/record_wakeword_clips.py --set train --only pos      # just one category
    python tools_dev/record_wakeword_clips.py --list-devices

Clips are 16 kHz mono WAV files in <out>/<set>/<category>/ (default out: C:\\Users\\<you>\\wakeword_train\\real). Each clip is announced
by a countdown and a beep; say the prompt shown when the beep sounds. Press Enter to start each category, Ctrl+C to stop (what was
recorded is kept; running again continues where it stopped). The audio never leaves this PC.
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

RATE = 16000
POS_PROMPTS = ["Hey Vex", "hey vex", "HEY VEX (louder)", "hey vex (quietly)", "Hey Vex (fast)", "hey... vex (slow)", "Hey Vex (from further away)"]
SPEECH_NEG = [
    "hey Alex", "hey tax", "hex", "vex", "hey Rex", "hey checks", "hey Becks", "hey Max", "hey Jax", "hey Kevin", "hey Siri", "hey Google",
    "okay Vex is not here", "heck yes", "fix it", "hey there", "hello", "hey Jarvis", "hey listen", "next", "text me", "what's next",
    "open notepad", "play some music", "what time is it", "set a timer", "turn the volume down", "good morning", "how are you",
    "I'll check the tax return", "the weather is nice today", "can you hear me", "let's go to lunch", "call mom", "send the email",
    "that was so funny", "I need to fix the bug", "hey, where are my keys", "have you seen my phone", "hey bro", "hey boss", "okay",
    "yes", "no", "wait a second", "never mind", "thanks a lot", "see you tomorrow", "what's up", "oh my god"]
CATEGORIES = {
    # name: (seconds per clip, count (train), count (heldout), instruction)
    "pos": (1.5, 50, 15, "say the phrase shown, once, when you hear the beep"),
    "neg_speech": (2.0, 100, 30, "say the phrase shown (similar-sounding and ordinary phrases)"),
    "neg_talk": (2.0, 60, 15, "talk about anything, normal conversation, never saying 'Vex'"),
    "neg_typing": (2.0, 30, 6, "type on the keyboard / click the mouse during each clip"),
    "neg_media": (2.0, 30, 6, "play music, a video or a podcast from your speakers (not Vex)"),
    "neg_silence": (2.0, 30, 6, "stay silent: room noise only"),
}


def beep(sd, np, freq: float = 880.0, ms: int = 120) -> None:
    t = np.arange(int(24000 * ms / 1000)) / 24000
    sd.play((0.25 * np.sin(2 * np.pi * freq * t)).astype("float32"), 24000)
    sd.wait()


def save_wav(path: Path, audio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["train", "heldout"], default="train")
    ap.add_argument("--only", choices=list(CATEGORIES), nargs="*", help="only these categories")
    ap.add_argument("--out", default=str(Path.home() / "wakeword_train" / "real"))
    ap.add_argument("--device", default=None, help="input device index or part of its name")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import numpy as np
    import sounddevice as sd

    if args.list_devices:
        print(sd.query_devices())
        return 0
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from voice.micstate import default_mic_muted

    if default_mic_muted():
        print("Windows has the microphone muted. Unmute it first (Settings > System > Sound > Input).")
        return 1

    out = Path(args.out) / args.set
    idx = 0 if args.set == "train" else 1
    for name, spec in CATEGORIES.items():
        if args.only and name not in args.only:
            continue
        secs, count = spec[0], spec[1 + idx]
        folder = out / name
        have = len(list(folder.glob("*.wav"))) if folder.exists() else 0
        if have >= count:
            print(f"[{name}] already has {have}/{count} clips, skipping")
            continue
        print(f"\n=== {name}: {count - have} clips left of {count} ({secs:g} s each) ===\n    {spec[3]}")
        input("    Press Enter when ready... ")
        for n in range(have, count):
            prompt = ""
            if name == "pos":
                prompt = POS_PROMPTS[n % len(POS_PROMPTS)]
            elif name == "neg_speech":
                prompt = SPEECH_NEG[(n + (0 if args.set == "train" else 7)) % len(SPEECH_NEG)]
            print(f"  [{n + 1}/{count}] {prompt}", end="", flush=True)
            for c in (3, 2, 1):
                print(f"  {c}", end="", flush=True)
                time.sleep(0.45)
            beep(sd, np)
            audio = sd.rec(int(RATE * secs), samplerate=RATE, channels=1, dtype="int16", device=device)
            sd.wait()
            peak = int(np.abs(audio).max())
            k = n
            while (folder / f"{name}_{args.set}_{k:03d}.wav").exists():          # never overwrite an earlier clip
                k += 1
            save_wav(folder / f"{name}_{args.set}_{k:03d}.wav", audio.ravel())
            level = "  (NO VOICE HEARD: speak right after the beep)" if peak < 300 and name in ("pos", "neg_speech", "neg_talk") else ""
            print(f"  ok, peak {peak}{level}")
        print(f"[{name}] done")
    print(f"\nAll done. Clips are in {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue.")
