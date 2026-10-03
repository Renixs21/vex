"""End-to-end wake-word check with a VIRTUAL microphone (for when nobody can speak to the real one).

The whole app runs for real (listener + openWakeWord model, recorder, Groq Whisper, agent, TTS, orb) except the microphone: this
replaces sounddevice.InputStream with a timeline into which speech made by edge-tts is injected. Both the wake-word stream and the
command recorder read the same timeline, exactly as they share the real microphone.

    python tools_dev/wakeword_sim.py            # wake sequence + self-wake test (speaks aloud, uses a few LLM requests)

Phase 1  "Hey Jarvis. What's the time?" in one breath -> expect: wake detected, chime, listening, recorded, transcript, reply, speech.
Phase 2  a long reply is spoken (then "read the rest") while "Hey Jarvis" is injected into the mic every few seconds, as if the
         speakers leaked into it at full strength -> expect: no wake while Vex is thinking or speaking.
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RATE = 16000


class Timeline:
    """Shared virtual audio: clips placed at absolute sample positions (16 kHz int16) over a quiet noise floor."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.clips: list[tuple[int, np.ndarray]] = []
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(1)

    def now_sample(self) -> int:
        return int((time.monotonic() - self.t0) * RATE)

    def inject(self, audio: np.ndarray, delay: float = 0.0) -> None:
        with self.lock:
            self.clips.append((self.now_sample() + int(delay * RATE), audio))

    def block(self, pos: int, n: int) -> np.ndarray:
        out = self.rng.normal(0, 3, n).astype(np.float32)
        with self.lock:
            for start, a in self.clips:
                lo, hi = max(pos, start), min(pos + n, start + len(a))
                if lo < hi:
                    out[lo - pos:hi - pos] += a[lo - start:hi - start]
        return np.clip(out, -32768, 32767).astype(np.int16)


class VirtualStream:
    """Drop-in for sounddevice.InputStream: delivers timeline blocks in real time."""

    timeline: Timeline

    def __init__(self, device=None, samplerate=RATE, channels=1, dtype="int16", blocksize=480, callback=None, **kw):
        assert samplerate == RATE and channels == 1, "the virtual microphone is 16 kHz mono"
        self.callback, self.blocksize = callback, blocksize
        self.active = False
        self._stop = threading.Event()

    def start(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True, name="virtual-mic").start()

    def _run(self):
        pos = VirtualStream.timeline.now_sample()
        while not self._stop.is_set():
            target = pos + self.blocksize
            while VirtualStream.timeline.now_sample() < target and not self._stop.is_set():
                time.sleep(0.002)
            self.callback(VirtualStream.timeline.block(pos, self.blocksize).reshape(-1, 1), self.blocksize, None, None)
            pos = target

    def stop(self):
        self._stop.set()
        self.active = False

    def close(self):
        self.stop()


def speech(text: str, voice: str = "en-US-GuyNeural") -> np.ndarray:
    """edge-tts speech as 16 kHz int16, scaled to a normal speaking level."""
    import edge_tts
    from scipy.signal import resample_poly

    from voice.tts import decode_audio

    async def synth():
        out = b""
        async for ch in edge_tts.Communicate(text, voice).stream():
            if ch["type"] == "audio":
                out += ch["data"]
        return out

    a = decode_audio(asyncio.run(synth()))
    x = resample_poly(a.samples, RATE, a.rate)
    x = x / max(1e-6, float(np.abs(x).max())) * 9000
    return x.astype(np.int16)


def main() -> int:
    import config
    import sounddevice as sd

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config.setup_logging(console=False)
    import logging

    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r)

    logging.getLogger().addHandler(Grab())
    VirtualStream.timeline = Timeline()
    sd.InputStream = VirtualStream
    tl = VirtualStream.timeline

    print("Preparing speech clips...", flush=True)
    wake_clip = speech("Hey Jarvis what's the time")
    leak_clip = speech("Hey Jarvis.")
    results: dict = {}

    def trace(since: int) -> list[str]:
        out = []
        for r in records[since:]:
            m = r.getMessage()
            if re.search(r"wake|trigger from|recording|speech recognition|speech started|speech ended|orb: state|seeded|chime|woke", m, re.I) \
                    and "orb: first" not in m:
                out.append(f"{time.strftime('%H:%M:%S', time.localtime(r.created))}.{int(r.msecs):03d} {r.name.split('.')[-1]:<10} {m[:120]}")
        return out

    def driver(parts: dict) -> None:
        try:
            ctl, wake = parts["controller"], parts["wakeword"]
            time.sleep(6)                                              # listener up and settled
            print("\n=== Phase 1: 'Hey Jarvis. What's the time?' in one breath ===", flush=True)
            mark = len(records)
            tl.inject(wake_clip)
            ctl.wait_idle(5.0)
            deadline = time.time() + 40
            while time.time() < deadline:
                time.sleep(0.5)
                if any("wake detected" in r.getMessage() for r in records[mark:]) and ctl.state == "idle" and \
                        any("speech ended" in r.getMessage() or "speech recognition result" in r.getMessage() for r in records[mark:]):
                    break
            time.sleep(1.0)
            results["phase1"] = trace(mark)
            results["phase1_wakes"] = sum("wake detected" in r.getMessage() for r in records[mark:])
            results["reply"] = ctl.last_reply

            print("\n=== Phase 2: long reply spoken while 'Hey Jarvis' leaks into the mic every 3.5 s ===", flush=True)
            time.sleep(4)
            mark = len(records)
            stop_leak = threading.Event()

            def leak():
                while not stop_leak.is_set():
                    tl.inject(leak_clip)
                    stop_leak.wait(3.5)

            def run_turn(text: str) -> None:
                """Submit text, wait until Vex is busy, leak 'Hey Jarvis' into the mic while it is, then wait until idle."""
                stop_leak.clear()
                for _ in range(20):
                    if ctl.submit_text(text) == "started":
                        break
                    time.sleep(1.0)
                t_end = time.time() + 20
                while ctl.state == "idle" and time.time() < t_end:
                    time.sleep(0.1)
                threading.Thread(target=leak, daemon=True).start()
                t_end = time.time() + 120
                while ctl.state != "idle" and time.time() < t_end:
                    time.sleep(0.5)
                stop_leak.set()

            run_turn("Tell me about the planet Mars in about eight sentences.")
            time.sleep(1.5)
            run_turn("read the rest")
            time.sleep(1.0)
            results["phase2"] = trace(mark)
            results["phase2_wakes"] = sum("wake detected" in r.getMessage() for r in records[mark:])
            results["interference"] = [r.getMessage() for r in records if re.search(r"trigger from (tray|hotkey|orb-click)", r.getMessage())]
        finally:
            parts["app"].quit()

    from ui.app import run

    threading.Thread(target=lambda: None).start()

    def on_ready(parts):
        threading.Thread(target=driver, args=(parts,), daemon=True).start()

    run(on_ready=on_ready)
    print("\n--- Phase 1 trace ---")
    print("\n".join(results.get("phase1", ["(none)"])))
    print(f"wakes: {results.get('phase1_wakes')}   reply: {results.get('reply')!r}")
    print("\n--- Phase 2 trace ---")
    print("\n".join(results.get("phase2", ["(none)"])))
    print(f"wakes while Vex was thinking/speaking with 'Hey Jarvis' leaking into the mic: {results.get('phase2_wakes')}")
    if results.get("interference"):
        print(f"WARNING: {len(results['interference'])} trigger(s) came from outside the simulation (tray/hotkey/click); the run may be invalid:")
        print("\n".join("  " + m for m in results["interference"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
