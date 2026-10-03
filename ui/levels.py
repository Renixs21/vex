"""Audio levels for the visual: RMS per ~33 ms chunk, and a pump that pushes them to the frontend ~30 times a second.

Python owns audio playback. Whoever plays audio (the sounddevice player below, or Phase 2's TTS player) computes the
levels from the same samples it plays and pumps them in step with playback.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np

from ui.frontend import LEVEL_HZ, clamp01, sleep_until

log = logging.getLogger("vex.levels")
HOP_S = 1.0 / LEVEL_HZ


def rms_levels(samples: np.ndarray, sample_rate: int, hop_s: float = HOP_S, gain: float = 4.0) -> list[float]:
    """Loudness 0..1 for each `hop_s` of audio (mono or multi-channel; floats in -1..1 or int16)."""
    x = np.asarray(samples)
    if x.size == 0 or sample_rate <= 0:
        return []
    if x.dtype.kind in "iu":
        x = x.astype(np.float32) / float(np.iinfo(x.dtype).max)
    x = x.astype(np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    hop = max(1, int(sample_rate * hop_s))
    out = []
    for i in range(0, len(x), hop):
        chunk = x[i:i + hop]
        out.append(clamp01(float(np.sqrt(np.mean(chunk * chunk))) * gain))
    return out


class LevelPump:
    """Calls `on_level(levels[i])` at the hop rate while audio plays, then `on_level(0)`.

    `position` returns seconds played so far (e.g. from the audio stream); without it, wall-clock time is used.
    Stops promptly when stop() is called.
    """

    def __init__(self, on_level: Callable[[float], None], levels: list[float], hop_s: float = HOP_S,
                 position: Callable[[], float] | None = None):
        self.on_level = on_level
        self.levels = levels
        self.hop_s = hop_s
        self.position = position
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="level-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        t0 = time.monotonic()
        n = 0
        try:
            while not self._stop.is_set():
                t = self.position() if self.position else time.monotonic() - t0
                i = int(t / self.hop_s)
                if i >= len(self.levels):
                    break
                self.on_level(self.levels[i])
                n += 1
                if sleep_until(t0 + n * self.hop_s, self._stop):            # absolute deadlines: no drift
                    break
        except Exception:  # noqa: BLE001
            log.exception("level pump failed")
        finally:
            self.on_level(0.0)


class SoundDevicePlayer:
    """Plays PCM audio through sounddevice and pumps levels in step with it. speak/stop are thread-safe."""

    def __init__(self, sd_module=None, device=None):
        self._sd = sd_module
        self.device = device                    # output device index or name part; None = the Windows default
        self._pump: LevelPump | None = None
        self._lock = threading.Lock()

    @property
    def sd(self):
        if self._sd is None:
            import sounddevice

            self._sd = sounddevice
        return self._sd

    def play(self, samples: np.ndarray, sample_rate: int, on_level: Callable[[float], None]) -> None:
        """Blocks until the audio ends or stop() is called."""
        x = np.asarray(samples)
        levels = rms_levels(x, sample_rate)
        with self._lock:
            self._pump = LevelPump(on_level, levels)
            if self.device is None:
                self.sd.play(x, sample_rate)
            else:
                self.sd.play(x, sample_rate, device=self.device)
            self._pump.start()
        try:
            self.sd.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        with self._lock:
            pump, self._pump = self._pump, None
        try:
            self.sd.stop()
        except Exception:  # noqa: BLE001
            log.debug("sounddevice stop failed", exc_info=True)
        if pump is not None:
            pump.stop()
