"""The reminder chime: two soft rising notes, a little longer than the wake blip so it is noticed from across the room."""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("vex.chime")


def make_reminder_chime(rate: int = 24000, volume: float = 0.22) -> np.ndarray:
    """E5 then A5, each with a smooth fade (no clicks), about 0.6 s in total. float32 mono."""
    notes = []
    for freq, ms in ((659.25, 260), (880.0, 380)):
        n = rate * ms // 1000
        t = np.arange(n) / rate
        env = np.sin(np.linspace(0, np.pi, n)) ** 1.5
        tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2 * t)
        notes.append((tone / 1.25 * env * volume).astype(np.float32))
    return np.concatenate(notes)


def play_reminder_chime(sd_module=None, blocking: bool = True) -> None:
    """Play the chime on the default output. Failure is logged and ignored: the reminder is still spoken."""
    try:
        sd = sd_module
        if sd is None:
            import sounddevice as sd  # noqa: PLC0415
        sd.play(make_reminder_chime(), 24000)
        if blocking:
            sd.wait()
    except Exception as e:  # noqa: BLE001
        log.debug("reminder chime failed: %s", e)
