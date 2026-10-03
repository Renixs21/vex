"""Wake-word calibration (a dev script, not an agent tool).

    python tools_dev/wakeword_tune.py --list-devices
    python tools_dev/wakeword_tune.py                 # live score bar for the active model (Ctrl+C to stop)
    python tools_dev/wakeword_tune.py --say 10        # say the phrase 10 times; counts detections at the current threshold
    python tools_dev/wakeword_tune.py --quiet 5       # listen 5 minutes while you talk / play a video; counts false wakes
    python tools_dev/wakeword_tune.py --say 10 --quiet 5 --write   # both, then offer to save the suggested threshold

Audio is scored in memory and never saved. Use --device "part of a name" to pick a microphone.
"""
from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from voice.recorder import FRAME, Recorder, to_frame  # noqa: E402
from voice.wakeword import CHUNK, _NoVad, _default_model_factory, label_for  # noqa: E402


def suggest_threshold(hit_peaks: list[float], false_peak: float | None) -> tuple[float, str]:
    """Pick a threshold between the loudest false wake and the quietest real one. Returns (value, explanation)."""
    lo = min(hit_peaks) if hit_peaks else None
    if lo is None and false_peak is None:
        return 0.5, "no data: keep the default 0.5"
    if lo is None:
        return round(min(0.9, max(0.5, (false_peak or 0) + 0.1)), 2), "no 'say it' results; set just above the loudest false wake"
    if false_peak is None:
        return round(max(0.3, min(0.7, lo * 0.7)), 2), "no false-wake test; set well below your quietest real wake"
    if lo > false_peak:
        mid = (lo + false_peak) / 2
        return round(min(0.9, max(0.3, mid)), 2), (f"your quietest real wake scored {lo:.2f}, the loudest false one {false_peak:.2f}: "
                                                    "the midpoint separates them")
    return round(min(0.9, false_peak + 0.05), 2), (f"overlap: a false wake ({false_peak:.2f}) scored higher than your quietest real one "
                                                     f"({lo:.2f}). This keeps false wakes out but you will miss some. Train or retry closer to the mic")


class Meter:
    """Feeds microphone audio to the model and yields (score) every 80 ms."""

    def __init__(self, device, model_path: str, fallback: str, vad: float):
        p = Path(model_path)
        p = p if p.is_absolute() else ROOT / p
        self.name = str(p) if p.is_file() else fallback
        self.label = label_for(self.name)
        self.model = _default_model_factory(self.name, vad)
        rec = Recorder(device=device or None, vad=_NoVad())
        self.frames: "queue.Queue[np.ndarray]" = queue.Queue()
        holder = {"rate": 16000}

        def cb(indata, n, ti, st):
            self.frames.put(to_frame(indata, holder["rate"]))

        self.stream, holder["rate"] = rec.open_stream(rec.resolve_device(), cb)
        self.buf = np.zeros(0, np.int16)

    def scores(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.buf = np.concatenate([self.buf, self.frames.get(timeout=0.2)])
            except queue.Empty:
                continue
            while len(self.buf) >= CHUNK:
                chunk, self.buf = self.buf[:CHUNK], self.buf[CHUNK:]
                yield max(self.model.predict(chunk).values(), default=0.0)

    def reset(self):
        self.model.reset()
        self.buf = np.zeros(0, np.int16)
        while not self.frames.empty():
            self.frames.get_nowait()

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


def bar(score: float, threshold: float, width: int = 40) -> str:
    filled = int(min(score, 1.0) * width)
    mark = int(threshold * width)
    cells = ["#" if i < filled else "." for i in range(width)]
    cells[min(mark, width - 1)] = "|" if cells[min(mark, width - 1)] == "." else "!"
    return "".join(cells)


def live(meter: Meter, threshold: float) -> None:
    print(f"Live scores for {meter.label}. '|' marks the threshold {threshold:.2f}. Ctrl+C to stop.")
    try:
        for s in meter.scores(10 ** 9):
            print(f"\r{s:4.2f} [{bar(s, threshold)}]" + ("  WAKE" if s >= threshold else "      "), end="", flush=True)
    except KeyboardInterrupt:
        print()


def say_test(meter: Meter, n: int, threshold: float) -> list[float]:
    print(f"\nSay the wake phrase ({meter.label}) when prompted, {n} times.")
    peaks = []
    for i in range(1, n + 1):
        input(f"  [{i}/{n}] press Enter, then say it... ")
        meter.reset()
        peak = max(meter.scores(3.5), default=0.0)
        peaks.append(peak)
        print(f"        peak score {peak:.2f}  {'DETECTED' if peak >= threshold else 'missed'}")
    hits = sum(p >= threshold for p in peaks)
    print(f"Detected {hits}/{n} at threshold {threshold:.2f}.")
    return peaks


def quiet_test(meter: Meter, minutes: float, threshold: float) -> float:
    print(f"\nListening for {minutes:g} min. Talk normally or play a video near the mic. Do NOT say the wake phrase.")
    meter.reset()
    peak, wakes, last = 0.0, 0, -1e9
    end = time.monotonic() + minutes * 60
    try:
        for s in meter.scores(minutes * 60):
            peak = max(peak, s)
            now = time.monotonic()
            if s >= threshold and now - last > 2.0:
                wakes, last = wakes + 1, now
                print(f"  false wake #{wakes} at {time.strftime('%H:%M:%S')} (score {s:.2f})")
    except KeyboardInterrupt:
        print("  stopped early")
    print(f"{wakes} false wake(s) at threshold {threshold:.2f}; loudest score heard {peak:.2f}.")
    return peak


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-devices", action="store_true", help="show input devices and exit")
    ap.add_argument("--device", default=None, help="input device index or part of its name")
    ap.add_argument("--say", type=int, metavar="N", help="say-it test: N attempts")
    ap.add_argument("--quiet", type=float, metavar="MIN", help="false-trigger test for MIN minutes")
    ap.add_argument("--threshold", type=float, help="threshold to test (default: from config.toml)")
    ap.add_argument("--write", action="store_true", help="offer to save the suggested threshold to config.toml")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list_devices:
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"{i:3d}  {d['name']}  ({d['max_input_channels']} ch, {int(d['default_samplerate'])} Hz)")
        return 0

    cfg = config.wakeword_config()
    threshold = args.threshold if args.threshold is not None else cfg["threshold"]
    device = args.device
    if device is not None and device.isdigit():
        device = int(device)
    device = device if device is not None else (cfg["input_device"] or config.settings["voice"].get("input_device") or None)
    try:
        meter = Meter(device, cfg["model_path"], cfg["fallback_model"], cfg["vad_threshold"])
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't start: {e}")
        return 1
    try:
        if args.say is None and args.quiet is None:
            live(meter, threshold)
            return 0
        hits = say_test(meter, args.say, threshold) if args.say else []
        false_peak = quiet_test(meter, args.quiet, threshold) if args.quiet else None
        value, why = suggest_threshold(hits, false_peak)
        print(f"\nSuggested threshold: {value}  ({why})")
        if args.write and input(f"Write threshold = {value} to config.toml? [y/N] ").strip().lower() == "y":
            config.set_toml_value("wakeword", "threshold", value)
            print("Saved. Restart Vex to use it.")
        return 0
    finally:
        meter.close()


if __name__ == "__main__":
    sys.exit(main())
