"""Text-to-speech: Google Cloud Chirp 3 HD first (when google-tts-key.json exists), edge-tts as the free fallback.

TTSChain tries providers in the order given in [voice] tts_order, skips one that failed recently, and counts Google characters per
month in data/voice_usage.json: near the free allowance it moves to edge-tts by itself (and says so once in the log). If every
provider fails the caller shows the text only. Text is sent to the TTS service, never stored.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from voice.usage import UsageStore

log = logging.getLogger("vex.tts")


@dataclass
class Audio:
    samples: np.ndarray          # float32 mono, -1..1
    rate: int


class TTSError(Exception):
    pass


class TTSUnavailable(TTSError):
    """No provider could produce speech."""


class TTSProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def synthesize(self, text: str) -> Audio: ...


def decode_audio(data: bytes) -> Audio:
    """MP3/WAV bytes -> mono float32 (libsndfile decodes MP3 without ffmpeg)."""
    import soundfile as sf

    samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if samples.size == 0:
        raise TTSError("the speech service returned no audio")
    return Audio(np.ascontiguousarray(samples), int(rate))


class EdgeTTS:
    """Microsoft Edge's online voices through the edge-tts package: free, no key. First audio in about a second."""

    name = "edge"

    def __init__(self, voice: str = "en-US-AriaNeural", communicate_factory: Callable | None = None):
        self.voice = voice
        self._factory = communicate_factory

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> Audio:
        async def run() -> bytes:
            if self._factory is not None:
                comm = self._factory(text, self.voice)
            else:
                import edge_tts

                comm = edge_tts.Communicate(text, self.voice)
            out = bytearray()
            async for chunk in comm.stream():
                if chunk.get("type") == "audio":
                    out += chunk["data"]
            return bytes(out)

        try:
            data = asyncio.run(asyncio.wait_for(run(), timeout=20))
        except Exception as e:  # noqa: BLE001 - offline, blocked, timeout
            raise TTSError(f"edge-tts failed: {type(e).__name__}: {e}") from e
        return decode_audio(data)


class GoogleTTS:
    """Google Cloud Text-to-Speech, Chirp 3 HD voices, as 24 kHz LINEAR16 (so there is nothing to decode).

    Needs a service-account key from the billing-enabled Google project (Project B in the README), at the path in
    GOOGLE_APPLICATION_CREDENTIALS (default google-tts-key.json). Without it `available()` is False and the chain skips it.
    """

    name = "google"

    def __init__(self, voice: str = "en-US-Chirp3-HD-Aoede", language: str = "en-US", credentials: Path | str | None = None,
                 client=None, sample_rate: int = 24000, validate_voice: bool = True):
        self.voice = voice
        self.language = language
        self.credentials = Path(credentials) if credentials else None
        self._client = client
        self.sample_rate = sample_rate
        self._validate = validate_voice
        self._validated = False

    def available(self) -> bool:
        return self._client is not None or (self.credentials is not None and self.credentials.is_file())

    @property
    def client(self):
        if self._client is None:
            from google.cloud import texttospeech

            self._client = texttospeech.TextToSpeechClient.from_service_account_file(str(self.credentials))
        return self._client

    def _check_voice(self) -> None:
        """Make sure the configured voice exists (once), so a typo falls back to edge-tts with a clear message."""
        if self._validated or not self._validate:
            return
        names = {v.name for v in self.client.list_voices(language_code=self.language).voices}
        if self.voice not in names:
            sample = ", ".join(sorted(n for n in names if "Chirp3-HD" in n)[:6])
            raise TTSError(f"voice {self.voice!r} not found for {self.language}. Chirp 3 HD voices include: {sample}")
        self._validated = True

    def synthesize(self, text: str) -> Audio:
        from google.cloud import texttospeech as t

        try:
            self._check_voice()
            resp = self.client.synthesize_speech(
                input=t.SynthesisInput(text=text),
                voice=t.VoiceSelectionParams(language_code=self.language, name=self.voice),
                audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.LINEAR16, sample_rate_hertz=self.sample_rate))
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001 - quota, auth, network
            raise TTSError(f"Google TTS failed: {type(e).__name__}: {e}") from e
        return decode_audio(resp.audio_content)


class TTSChain:
    def __init__(self, providers: list[TTSProvider], usage: UsageStore | None = None, monthly_limit: float = 1_000_000,
                 switch_fraction: float = 0.9, retry_after: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.providers = providers
        self.usage = usage
        self.monthly_limit = monthly_limit
        self.switch_fraction = switch_fraction
        self.retry_after = retry_after
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._announced_budget: str | None = None
        self.last_provider: str | None = None

    def google_budget_left(self) -> float | None:
        return None if self.usage is None else self.monthly_limit - self.usage.month_total("google_chars")

    def _skip_reason(self, p: TTSProvider, text: str) -> str | None:
        if not p.available():
            return "not configured"
        if self._clock() < self._down_until.get(p.name, 0):
            return "failed recently"
        if p.name == "google" and self.usage is not None:
            used = self.usage.month_total("google_chars")
            if used >= self.monthly_limit * self.switch_fraction or used + len(text) > self.monthly_limit:
                month = self.usage._month()
                if self._announced_budget != month:
                    self._announced_budget = month
                    log.warning("Google TTS is at %d of %d free characters this month; using edge-tts until it resets",
                                used, self.monthly_limit)
                return "monthly allowance"
        return None

    def synthesize(self, text: str) -> Audio:
        last: Exception | None = None
        for p in self.providers:
            reason = self._skip_reason(p, text)
            if reason:
                log.debug("TTS %s skipped (%s)", p.name, reason)
                continue
            try:
                audio = p.synthesize(text)
            except Exception as e:  # noqa: BLE001
                last = e
                self._down_until[p.name] = self._clock() + self.retry_after
                log.warning("TTS %s failed (%s); trying the next provider", p.name, e)
                continue
            if p.name == "google" and self.usage is not None:
                self.usage.add_month("google_chars", len(text))
            self.last_provider = p.name
            return audio
        raise TTSUnavailable(str(last) if last else "no text-to-speech provider is available")
