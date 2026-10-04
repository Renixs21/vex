"""Copy YOUR recorded clips into the livekit-wakeword training folders (run after `generate`, before `augment`).

Real positives are trimmed to the spoken phrase and repeated POS_COPIES times (augmentation makes each copy different).
Real negatives (speech, talk, typing, media, silence) are repeated NEG_COPIES times. Held-out clips are never touched.
"""
import glob
import shutil
import sys
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

OUT = Path(r"C:\Users\sanja\wakeword_train\output\hey_vex")
REAL = Path(r"C:\Users\sanja\wakeword_train\real\train")
POS_COPIES, NEG_COPIES = 30, 8
RATE = 16000


def trim(a: np.ndarray, pad: float = 0.12) -> np.ndarray:
    """Cut leading/trailing silence so the phrase sits at the clip end like the synthetic positives."""
    env = np.convolve(np.abs(a.astype(np.float32)), np.ones(320) / 320, mode="same")
    loud = np.where(env > max(0.08 * env.max(), 150))[0]
    if len(loud) == 0:
        return a
    s, e = max(0, loud[0] - int(pad * RATE)), min(len(a), loud[-1] + int(pad * RATE))
    return a[s:e]


def next_index(d: Path) -> int:
    nums = [int(p.stem.split("_")[1]) for p in d.glob("clip_[0-9][0-9][0-9][0-9][0-9][0-9].wav")]
    return max(nums, default=-1) + 1


def add(files, dest: Path, copies: int, trimmed: bool) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    n = next_index(dest)
    count = 0
    for f in files:
        w = wave.open(str(f))
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        w.close()
        if trimmed:
            if np.abs(a).max() < 300:
                continue
            a = trim(a)
        x = (a.astype(np.float32) / 32768.0)
        for _ in range(copies):
            sf.write(str(dest / f"clip_{n:06d}.wav"), x, RATE)
            n += 1
            count += 1
    return count


pos = sorted(glob.glob(str(REAL / "pos" / "*.wav")))
negs = [f for c in ("neg_speech", "neg_talk", "neg_typing", "neg_media", "neg_silence") for f in sorted(glob.glob(str(REAL / c / "*.wav")))]
print("real positives:", len(pos), "-> added", add(pos, OUT / "positive_train", POS_COPIES, True))
print("real negatives:", len(negs), "-> added", add(negs, OUT / "negative_train", NEG_COPIES, False))
