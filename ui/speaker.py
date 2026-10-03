"""Speaker interface: the controller says a reply through this and the visual gets levels from it.

Phase 2 provides the real implementation (TTS audio played through ui.levels.SoundDevicePlayer). Until then
SimulatedSpeaker fakes speech timing and a syllable-like loudness envelope, so the whole UI flow can be exercised.
"""
from __future__ import annotations

import math
import random
import threading
import time
from typing import Callable, Protocol

from ui.frontend import LEVEL_HZ, sleep_until


class Speaker(Protocol):
    def speak(self, text: str, on_level: Callable[[float], None]) -> None:
        """Say `text`, calling on_level(0..1) about LEVEL_HZ times a second. Blocks until finished or stop()ped."""

    def stop(self) -> None:
        """Stop speaking immediately (safe to call from any thread, and when nothing is playing)."""


class SimulatedSpeaker:
    """No audio: waits as long as the text would take to say and emits a fake speech envelope."""

    def __init__(self, words_per_second: float = 3.2, max_seconds: float = 20.0):
        self.words_per_second = words_per_second
        self.max_seconds = max_seconds
        self._stop = threading.Event()

    def duration(self, text: str) -> float:
        return min(self.max_seconds, max(0.8, len(text.split()) / self.words_per_second))

    def speak(self, text: str, on_level: Callable[[float], None]) -> None:
        self._stop.clear()
        end = time.monotonic() + self.duration(text)
        t0 = time.monotonic()
        n = 0
        try:
            while not self._stop.is_set() and time.monotonic() < end:
                t = time.monotonic() - t0
                n += 1
                syll = max(0.0, math.sin(t * 9.3) * 0.6 + math.sin(t * 4.1 + 1) * 0.4)
                phrase = 0.55 + 0.45 * math.sin(t * 0.9)
                on_level(min(1.0, syll * phrase + random.random() * 0.08))
                sleep_until(t0 + n / LEVEL_HZ, self._stop)
        finally:
            on_level(0.0)

    def stop(self) -> None:
        self._stop.set()
