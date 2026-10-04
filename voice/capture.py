"""VoiceCapture: microphone -> recorder -> Whisper -> text. Implements the controller's Capture interface.

If speech can't be used right now (no microphone, Windows blocking mic access, Whisper's allowance used up or unreachable) it
falls back to the typed prompt and tells the user once why, so a limit or a missing mic never leaves you stuck.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from voice.micstate import default_mic_muted
from voice.recorder import NEAR_SILENT_RMS, MicUnavailable, Recorder
from voice.stt import GroqWhisper, SttLimitReached, SttUnavailable

log = logging.getLogger("vex.capture")
NOTICE_EVERY = 600.0                 # seconds between repeats of the same notice


class VoiceCapture:
    def __init__(self, recorder: Recorder, stt: GroqWhisper, fallback: Callable[[], str | None] | None = None,
                 notify: Callable[[str, str], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self.recorder = recorder
        self.stt = stt
        self.fallback = fallback
        self.notify = notify
        self._clock = clock
        self._cancel = threading.Event()
        self._noticed: dict[str, float] = {}
        self._told_near_silent = False

    def __call__(self) -> str | None:
        self._cancel = threading.Event()
        if not self.stt.available:
            return self._typed("noKey", "Speech recognition isn't set up (no GROQ_API_KEY), so type your command instead.")
        try:
            rec = self.recorder.record(self._cancel)
        except MicUnavailable as e:
            log.warning("microphone unavailable: %s", e)
            return self._typed("mic", "Vex can't use the microphone. Check Windows Settings > Privacy > Microphone "
                                      "(allow desktop apps). Type your command instead.")
        if rec.peak_rms < NEAR_SILENT_RMS and rec.reason != "cancelled":
            muted = default_mic_muted()
            log.warning("mic is near-silent (RMS %.1f): nothing louder than this was heard%s", rec.peak_rms,
                        "; WINDOWS HAS THE MICROPHONE MUTED" if muted else "")
            if (muted or not self._told_near_silent) and self.notify and self._clock() - self._noticed.get("silent", -1e9) > 60:
                self._told_near_silent = True                                        # a muted mic is told every minute, not once
                self._noticed["silent"] = self._clock()
                try:
                    why = ("Windows has the microphone muted (unmute it in Settings > System > Sound > Input, or the laptop's mic key)."
                           if muted else "Check that it isn't muted, its volume in Windows Sound settings, or Settings > Privacy > "
                                         "Microphone.")
                    self.notify("Vex can't hear you", f"The microphone is almost silent (RMS {rec.peak_rms:.1f}). {why} You can also type instead.")
                except Exception:  # noqa: BLE001
                    log.debug("notify failed", exc_info=True)
        if rec.reason == "cancelled" or self._cancel.is_set():
            return None
        if rec.reason == "no_speech" or len(rec.audio) == 0:
            return None
        try:
            text = self.stt.transcribe(rec.audio) or None
            log.info("speech recognition result: %r", text or "")
            return text
        except SttLimitReached as e:
            until = datetime.fromtimestamp(time.time() + e.wait).strftime("%H:%M")
            return self._typed("limit", f"Speech recognition is rate limited until about {until}. Type your command instead.")
        except SttUnavailable as e:
            log.warning("speech recognition unavailable: %s", e)
            return self._typed("stt", "I couldn't reach speech recognition. Type your command instead.")

    def cancel(self) -> None:
        self._cancel.set()
        if self.fallback is not None and hasattr(self.fallback, "cancel"):
            try:
                self.fallback.cancel()
            except Exception:  # noqa: BLE001
                log.debug("fallback cancel failed", exc_info=True)

    def _typed(self, reason: str, message: str) -> str | None:
        now = self._clock()
        if self.notify and now - self._noticed.get(reason, -1e9) > NOTICE_EVERY:
            self._noticed[reason] = now
            try:
                self.notify("Vex", message)
            except Exception:  # noqa: BLE001
                log.debug("notify failed", exc_info=True)
        return self.fallback() if self.fallback is not None else None
