"""Speech-to-text: Groq's hosted Whisper first, Gemini as the backup.

  * Groq gets a short deadline (`timeout` plus a little per second of audio, 3 s by default) and one retry on a timeout or a server error;
    if it still can't answer, or says 429, the same audio goes to the backup provider (SttChain), so a slow or limited Whisper no longer
    means a wait of many seconds or typing the command instead.

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
from voice.timeline import timeline
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
    name = "groq"

    def __init__(self, api_key: str = "", model: str = "whisper-large-v3-turbo", language: str = "en", prompt: str = "",
                 usage: UsageStore | None = None, seconds_per_hour: float = 7200, client=None,
                 clock: Callable[[], float] = time.time, timeout: float = 3.0, timeout_per_second: float = 0.25, retries: int = 1):
        self.timeout = timeout                    # the deadline for one attempt: timeout + timeout_per_second * seconds of audio
        self.timeout_per_second = timeout_per_second
        self.retries = max(0, retries)
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
        resp = self._call(kwargs, self.timeout + self.timeout_per_second * seconds)
        if self.usage is not None:
            self.usage.add_event("whisper", seconds)
        return self._clean(resp, seconds)

    def _call(self, kwargs: dict, timeout: float = 15.0):
        attempts = 1 + self.retries
        for attempt in range(1, attempts + 1):
            try:
                timeline.set(stt_attempts=attempt)
                return self.client.audio.transcriptions.create(**kwargs, timeout=timeout)
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
                if transient and attempt < attempts:
                    log.info("Groq Whisper attempt %d failed (%s); retrying once", attempt, type(e).__name__)
                    time.sleep(0.1)
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


class GeminiSTT:
    """Backup speech recognition: Gemini transcribes the audio (free tier, the key you already use for the brain)."""

    name = "gemini"
    PROMPT = ("Transcribe this audio exactly as spoken. Reply with only the words that were said, nothing else: no quotes, no notes. "
              "If there is no speech, reply with nothing at all.")

    def __init__(self, api_key: str = "", model: str = "", language: str = "en", timeout: float = 8.0, client=None):
        self.model = model
        self.language = language
        self.timeout = timeout
        self._client = client
        self._api_key = api_key

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._api_key and self.model)

    @property
    def client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(api_key=self._api_key, http_options=types.HttpOptions(
                timeout=int(self.timeout * 1000), retry_options=types.HttpRetryOptions(attempts=1)))
        return self._client

    def transcribe(self, audio: np.ndarray, rate: int = RATE) -> str:
        audio = np.asarray(audio)
        if len(audio) / float(rate) < 0.2:
            return ""
        try:
            from google.genai import types

            resp = self.client.models.generate_content(
                model=self.model, contents=[types.Part.from_bytes(data=wav_bytes(audio, rate), mime_type="audio/wav"), self.PROMPT])
            text = (getattr(resp, "text", None) or "").strip().strip('"').strip()
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "code", None) or getattr(e, "status_code", None)
            if status == 429:
                raise SttLimitReached(60.0, "Gemini is rate-limiting speech recognition") from e
            raise SttUnavailable(f"backup speech recognition failed: {type(e).__name__}") from e
        if not text or _norm(text) in HALLUCINATIONS and len(audio) / float(rate) < 1.5:
            return ""
        return text


class SttChain:
    """The speech recognition the rest of Vex uses: providers in order. A provider that errors, times out or is rate-limited hands the same
    audio to the next one; only when all fail does the caller see an error (and falls back to typing)."""

    def __init__(self, providers: list):
        self.providers = providers

    @property
    def available(self) -> bool:
        return any(p.available for p in self.providers)

    @property
    def model(self) -> str:
        first = next((p for p in self.providers if p.available), self.providers[0])
        return first.model

    @property
    def usage(self):
        return getattr(self.providers[0], "usage", None)

    def remaining_seconds(self):
        return self.providers[0].remaining_seconds() if hasattr(self.providers[0], "remaining_seconds") else None

    def transcribe(self, audio: np.ndarray, rate: int = RATE) -> str:
        first_error: SttError | None = None
        last: SttError | None = None
        for i, p in enumerate(self.providers):
            if not p.available:
                continue
            try:
                text = p.transcribe(audio, rate)
                timeline.set(stt_provider=p.name, stt_fallback=i > 0 or None)
                return text
            except SttError as e:
                first_error = first_error or e
                last = e
                log.warning("speech recognition via %s failed (%s)%s", p.name, e,
                            "; trying the backup" if i + 1 < len(self.providers) else "")
        if first_error is not None and isinstance(first_error, SttLimitReached):
            raise first_error                                       # keep "try again in N s" for the typed-fallback notice
        raise last or SttUnavailable("no speech recognition is configured")
