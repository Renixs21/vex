"""VoiceCapture: microphone -> recorder -> Whisper -> text. Implements the controller's Capture interface.

If speech can't be used right now (no microphone, Windows blocking mic access, Whisper's allowance used up or unreachable) it
falls back to the typed prompt and tells the user once why, so a limit or a missing mic never leaves you stuck.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Callable

import numpy as np

from voice.micstate import default_mic_muted
from voice.recorder import NEAR_SILENT_RMS, MicUnavailable, Recorder
from voice.stt import GroqWhisper, SttLimitReached, SttUnavailable
from voice.timeline import timeline

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
        return self._capture()

    def after_wake(self, seconds: float) -> str | None:
        """Right after the wake word: a normal capture, but if no command starts within `seconds` it gives up quietly so Vex can say
        its greeting and listen again (instead of six silent seconds)."""
        return self._capture(no_speech_seconds=seconds, wake=True)

    def listen_followup(self, seconds: float, min_speech_ms: int = 250) -> str | None:
        """The follow-up window after a reply: listen up to `seconds` for speech to START (no wake word needed). Same endpointing and
        pipeline as a normal turn once you talk. Stricter start (`min_speech_ms` of voiced audio) so a cough or the room doesn't count.
        Quiet on failure: None (no typed prompt, no "can't hear you" toast), because you didn't ask for anything."""
        return self._capture(no_speech_seconds=seconds, min_speech_ms=min_speech_ms, followup=True)

    def _capture(self, no_speech_seconds: float | None = None, min_speech_ms: int | None = None, followup: bool = False,
                 wake: bool = False) -> str | None:
        self._cancel = threading.Event()
        if not self.stt.available:
            if followup:
                return None
            return self._typed("noKey", "Speech recognition isn't set up (no GROQ_API_KEY), so type your command instead.")
        timeline.mark("capture_start")
        try:
            if followup:
                rec = self.recorder.record(self._cancel, no_speech_seconds=no_speech_seconds, min_speech_ms=min_speech_ms)
            elif wake:
                rec = self.recorder.record(self._cancel, no_speech_seconds=no_speech_seconds)
            else:
                rec = self.recorder.record(self._cancel)
        except MicUnavailable as e:
            log.warning("microphone unavailable: %s", e)
            if followup:
                return None
            return self._typed("mic", "Vex can't use the microphone. Check Windows Settings > Privacy > Microphone "
                                      "(allow desktop apps). Type your command instead.")
        if rec.peak_rms < NEAR_SILENT_RMS and rec.reason != "cancelled" and not followup:
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
        timeline.set(endpoint_ms=getattr(self.recorder, "silence_ms", None), grace_ms=getattr(self.recorder, "grace_ms", None))
        if rec.t_start:
            timeline.mark("record_start", rec.t_start)
            timeline.mark("speech_end", rec.t_last_voiced or rec.t_end)
            timeline.mark("record_end", rec.t_end)
        if rec.reason == "cancelled" or self._cancel.is_set():
            if rec.resume is not None:
                rec.resume.stop()
            return None
        if rec.reason == "no_speech" or len(rec.audio) == 0:
            return None
        try:
            text = self._transcribe(rec) or None
            log.info("speech recognition result: %r", text or "")
            if text:
                timeline.set(words=len(text.split()), ends_clean=bool(re.search(r"[.!?][\"')\]]?$", text.strip())))
            return text
        except SttLimitReached as e:
            until = datetime.fromtimestamp(time.time() + e.wait).strftime("%H:%M")
            return self._typed("limit", f"Speech recognition is rate limited until about {until}. Type your command instead.")
        except SttUnavailable as e:
            log.warning("speech recognition unavailable: %s", e)
            return self._typed("stt", "I couldn't reach speech recognition. Type your command instead.")

    def _stt(self, audio: np.ndarray, discard: threading.Event | None = None) -> str:
        """One transcription, with its start and end on the latency timeline (a discarded one leaves no end mark)."""
        timeline.mark("stt_start")
        timeline.set(audio_seconds=round(len(audio) / 16000, 2))
        text = self.stt.transcribe(audio)
        if discard is None or not discard.is_set():
            timeline.mark("stt_end", overwrite=True)
        return text

    def _transcribe(self, rec) -> str:
        """Speech to text. If you carried on talking right after the endpoint (the mid-sentence-pause safeguard) both parts are
        transcribed together; otherwise the recognition that was already running is simply awaited, so the safeguard costs no time."""
        probe = rec.resume
        if probe is None:
            return self._stt(rec.audio)
        result: dict = {}
        discard = threading.Event()

        def first() -> None:
            try:
                result["text"] = self._stt(rec.audio, discard)
            except BaseException as e:  # noqa: BLE001 - re-raised below, in the calling thread
                result["err"] = e

        worker = threading.Thread(target=first, name="stt-first", daemon=True)
        worker.start()
        try:
            extra = probe.wait(timeout=probe.grace_ms / 1000 + probe.max_extra_s + 2)
        finally:
            probe.stop()
        if extra is None:
            worker.join()
            if "err" in result:
                raise result["err"]
            return result.get("text", "")
        discard.set()                                             # the first recognition is out of date: you were not done
        log.info("you carried on after a pause of %.2fs: transcribing both parts together", probe.gap_s or 0.0)
        timeline.set(resumed=True, resume_gap=probe.gap_s)
        merged = np.concatenate([rec.audio, np.zeros(int(0.25 * 16000), np.int16), extra])
        return self._stt(merged)

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
