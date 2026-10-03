"""Speech-to-text through Groq's hosted Whisper.

  * Audio goes to Groq only after you trigger listening (hotkey, tray, click) and is sent from memory: never written to disk.
  * The language defaults to English (more accurate); set [voice] language = "" to auto-detect (Hindi, Kannada...).
  * Seconds of audio are counted per hour in data/voice_usage.json. When the local allowance is used up, or Groq answers 429,
    SttLimitReached tells the caller how long to wait so Vex can fall back to typing instead of failing mid-sentence.
  * Whisper tends to "hear" phrases like "Thank you." in silence; those are dropped using its own no-speech scores.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np

from brain.llm import retry_after
from voice.recorder import RATE, wav_bytes
from voice.usage import UsageStore

log = logging.getLogger("vex.stt")

HOUR = 3600.0
HALLUCINATIONS = frozenset({"thank you", "thanks for watching", "thanks for listening", "thank you for watching", "you", "bye",
                            "bye bye", "okay", "ok", "uh", "um", "hmm", "mm-hmm", "so", "yeah"})


class SttError(Exception):
    """Speech recognition failed."""


class SttLimitReached(SttError):
    """Whisper's allowance is used up (locally tracked, or Groq said 429). `wait` is seconds until it frees up."""

    def __init__(self, wait: float, message: str = ""):
        super().__init__(message or f"speech recognition limit reached; try again in {wait:.0f} s")
        self.wait = wait


class SttUnavailable(SttError):
    """Speech recognition can't be reached (no key, offline, server error)."""


def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() or c in "' -" else " " for c in text).split())


def _seg(seg, key: str, default: float = 0.0) -> float:
    try:
        v = seg[key] if isinstance(seg, dict) else getattr(seg, key)
        return float(v)
    except (KeyError, AttributeError, TypeError, ValueError):
        return default


class GroqWhisper:
    def __init__(self, api_key: str = "", model: str = "whisper-large-v3-turbo", language: str = "en", prompt: str = "",
                 usage: UsageStore | None = None, seconds_per_hour: float = 7200, client=None,
                 clock: Callable[[], float] = time.time):
        self.model = model
        self.language = language
        self.prompt = prompt
        self.usage = usage
        self.seconds_per_hour = seconds_per_hour
        self._client = client
        self._api_key = api_key
        self._clock = clock
        self.blocked_until = 0.0

    @property
    def client(self):
        if self._client is None:
            if not self._api_key:
                raise SttUnavailable("no GROQ_API_KEY configured")
            from groq import Groq

            self._client = Groq(api_key=self._api_key, timeout=15.0, max_retries=0)       # Vex controls retries
        return self._client

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._api_key)

    def remaining_seconds(self) -> float | None:
        """Whisper seconds left in the local hourly allowance (None if untracked)."""
        return None if self.usage is None else max(0.0, self.seconds_per_hour - self.usage.window_total("whisper", HOUR))

    def _check_allowance(self, seconds: float) -> None:
        now = self._clock()
        if now < self.blocked_until:
            raise SttLimitReached(self.blocked_until - now, "Groq is rate-limiting speech recognition")
        if self.usage is not None:
            wait = self.usage.seconds_until_room("whisper", HOUR, self.seconds_per_hour, seconds)
            if wait > 0:
                raise SttLimitReached(wait, "the hourly speech recognition allowance is used up")

    def transcribe(self, audio: np.ndarray, rate: int = RATE) -> str:
        """Text of the recording ("" if nothing intelligible). Raises SttLimitReached or SttUnavailable."""
        audio = np.asarray(audio)
        seconds = len(audio) / float(rate)
        if seconds < 0.2:
            return ""
        self._check_allowance(seconds)
        kwargs: dict = {"file": ("speech.wav", wav_bytes(audio, rate)), "model": self.model, "response_format": "verbose_json",
                        "temperature": 0.0}
        if self.language:
            kwargs["language"] = self.language
        if self.prompt:
            kwargs["prompt"] = self.prompt
        resp = self._call(kwargs)
        if self.usage is not None:
            self.usage.add_event("whisper", seconds)
        return self._clean(resp, seconds)

    def _call(self, kwargs: dict):
        for attempt in (1, 2):
            try:
                return self.client.audio.transcriptions.create(**kwargs)
            except SttError:
                raise
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None) or getattr(e, "code", None)
                if status == 429:
                    wait = retry_after(e) or 60.0
                    self.blocked_until = self._clock() + wait
                    log.warning("Groq Whisper rate limit: waiting %.0f s", wait)
                    raise SttLimitReached(wait, "Groq is rate-limiting speech recognition") from e
                transient = status in (500, 502, 503, 504) or any(
                    w in type(e).__name__.lower() for w in ("connect", "timeout"))
                if transient and attempt == 1:
                    time.sleep(0.4)
                    continue
                raise SttUnavailable(f"speech recognition failed: {type(e).__name__}") from e
        raise SttUnavailable("speech recognition failed")

    @staticmethod
    def _clean(resp, seconds: float) -> str:
        text = (getattr(resp, "text", None) if not isinstance(resp, dict) else resp.get("text")) or ""
        text = text.strip()
        segments = (getattr(resp, "segments", None) if not isinstance(resp, dict) else resp.get("segments")) or []
        if not text:
            return ""
        probs = [_seg(s, "no_speech_prob") for s in segments]
        if probs and min(probs) > 0.6:                                   # Whisper itself says there was no speech
            return ""
        if _norm(text) in HALLUCINATIONS and (seconds < 1.5 or (probs and max(probs) > 0.3)):
            return ""
        return text
