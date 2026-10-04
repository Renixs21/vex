"""Text-to-speech ladder: Google Cloud Chirp 3 HD, then edge-tts (free), then the offline Windows voice, so Vex is never silent.

Google needs a key: GOOGLE_TTS_API_KEY (an API key restricted to the Text-to-Speech API) or a service-account file
(google-tts-key.json); without either it is skipped. TTSChain tries providers in the order of [voice] tts_order and counts Google
characters per month in data/voice_usage.json: it warns (tray) at 75% and 90% of the free allowance and moves to edge-tts by itself at
90%. A quota or billing error pauses Google for an hour, a rejected key switches it off for the session (one notification either
way); a network blip retries after a minute. Text is sent to the TTS service, never stored. The key is never logged.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import subprocess
import sys
import tempfile
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


class TTSQuotaError(TTSError):
    """The provider's quota or billing limit was hit: stop using it for a while (an hour), not a minute."""


class TTSAuthError(TTSError):
    """The key or project setup was rejected: stop using the provider for this session and say so once."""


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


API_URL = "https://texttospeech.googleapis.com/v1"


def _classify(status: int | None, message: str, code_name: str = "") -> type[TTSError]:
    """Map a Google error (HTTP status / gRPC name / message) to quota, auth or a plain failure."""
    m = f"{code_name} {message}".lower()
    squashed = m.replace("_", "").replace(" ", "")
    if status == 429 or "resourceexhausted" in squashed or "quota" in m or "rate limit" in m:
        return TTSQuotaError
    if status in (400, 401, 403) or any(w in m for w in ("api key not valid", "permission", "unauthenticated", "billing", "disabled",
                                                           "invalidargument", "credentials")):
        return TTSAuthError
    return TTSError


class GoogleTTS:
    """Google Cloud Text-to-Speech, Chirp 3 HD voices, as 24 kHz LINEAR16 (so there is nothing to decode).

    Two ways to authenticate (an API key wins if both exist): `api_key` (REST, the key goes in a header and is never logged) or a
    service-account file at `credentials` (the google-cloud-texttospeech SDK). Both need the billing-enabled project (Project B in
    the README). Without either, `available()` is False and the chain skips it.
    """

    name = "google"

    def __init__(self, voice: str = "en-US-Chirp3-HD-Aoede", language: str = "en-US", credentials: Path | str | None = None,
                 client=None, sample_rate: int = 24000, validate_voice: bool = True, api_key: str | None = None,
                 transport=None):
        self.voice = voice
        self.language = language
        self.credentials = Path(credentials) if credentials else None
        self._client = client
        self.sample_rate = sample_rate
        self._validate = validate_voice
        self._validated = False
        self.api_key = (api_key or "").strip() or None
        self._transport = transport                     # httpx transport (tests mock the network here)

    def available(self) -> bool:
        return (self._client is not None or bool(self.api_key)
                or (self.credentials is not None and self.credentials.is_file()))

    @property
    def mode(self) -> str:
        if self._client is not None:
            return "sdk"
        return "api-key" if self.api_key else "service-account"

    @property
    def client(self):
        if self._client is None:
            from google.cloud import texttospeech

            self._client = texttospeech.TextToSpeechClient.from_service_account_file(str(self.credentials))
        return self._client

    # ---- helpers -------------------------------------------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "[key]") if self.api_key else text

    def _http(self):
        import httpx

        return httpx.Client(timeout=20, transport=self._transport, headers={"X-Goog-Api-Key": self.api_key or ""})

    def _raise_http(self, resp) -> None:
        try:
            err = resp.json().get("error", {})
        except Exception:  # noqa: BLE001
            err = {}
        msg = self._scrub(str(err.get("message") or resp.text[:200]))
        cls = _classify(resp.status_code, msg, str(err.get("status", "")))
        raise cls(f"Google TTS HTTP {resp.status_code}: {msg}")

    def voice_names(self) -> set[str]:
        """Names of the voices Google offers for the language."""
        if self.mode == "api-key":
            with self._http() as http:
                resp = http.get(f"{API_URL}/voices", params={"languageCode": self.language})
            if resp.status_code != 200:
                self._raise_http(resp)
            return {v["name"] for v in resp.json().get("voices", [])}
        return {v.name for v in self.client.list_voices(language_code=self.language).voices}

    def _check_voice(self) -> None:
        """Make sure the configured voice exists (once), so a typo falls back to edge-tts with a clear message."""
        if self._validated or not self._validate:
            return
        names = self.voice_names()
        if self.voice not in names:
            sample = ", ".join(sorted(n for n in names if "Chirp3-HD" in n)[:6])
            raise TTSError(f"voice {self.voice!r} not found for {self.language}. Chirp 3 HD voices include: {sample}")
        self._validated = True

    # ---- synthesis -----------------------------------------------------------------------------------------------------
    def synthesize(self, text: str) -> Audio:
        try:
            self._check_voice()
            data = self._synth_rest(text) if self.mode == "api-key" else self._synth_sdk(text)
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001 - quota, auth, network
            name = type(e).__name__
            code = getattr(e, "code", None)
            cls = _classify(code if isinstance(code, int) else None, self._scrub(str(e)), name)
            raise cls(f"Google TTS failed: {name}: {self._scrub(str(e))[:200]}") from e
        return decode_audio(data)

    def _synth_rest(self, text: str) -> bytes:
        body = {"input": {"text": text}, "voice": {"languageCode": self.language, "name": self.voice},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": self.sample_rate}}
        with self._http() as http:
            resp = http.post(f"{API_URL}/text:synthesize", json=body)
        if resp.status_code != 200:
            self._raise_http(resp)
        return base64.b64decode(resp.json()["audioContent"])

    def _synth_sdk(self, text: str) -> bytes:
        from google.cloud import texttospeech as t

        resp = self.client.synthesize_speech(
            input=t.SynthesisInput(text=text),
            voice=t.VoiceSelectionParams(language_code=self.language, name=self.voice),
            audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.LINEAR16, sample_rate_hertz=self.sample_rate))
        return resp.audio_content


class WindowsTTS:
    """The voice built into Windows (System.Speech, offline). Not pretty, but it works with no internet and no key: the last rung."""

    name = "windows"

    def __init__(self, voice: str = "", run=None):
        self.voice = voice
        self._run = run or subprocess.run

    def available(self) -> bool:
        return sys.platform == "win32"

    def synthesize(self, text: str) -> Audio:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "if ($env:VEX_VOICE) { try { $s.SelectVoice($env:VEX_VOICE) } catch {} }; "
                  "$s.SetOutputToWaveFile($env:VEX_WAV); $s.Speak($env:VEX_TEXT); $s.Dispose()")
        try:
            self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30, check=True,
                      capture_output=True, env={**os.environ, "VEX_TEXT": text, "VEX_WAV": path, "VEX_VOICE": self.voice},
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return decode_audio(Path(path).read_bytes())
        except TTSError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"Windows voice failed: {type(e).__name__}: {e}") from e
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class TTSChain:
    def __init__(self, providers: list[TTSProvider], usage: UsageStore | None = None, monthly_limit: float = 1_000_000,
                 switch_fraction: float = 0.9, retry_after: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 notify: Callable[[str, str], None] | None = None, warn_fractions: tuple[float, ...] = (0.75, 0.9),
                 quota_pause: float = 3600.0):
        self.providers = providers
        self.usage = usage
        self.monthly_limit = monthly_limit
        self.switch_fraction = switch_fraction
        self.retry_after = retry_after
        self.quota_pause = quota_pause
        self.notify = notify
        self.warn_fractions = tuple(sorted(warn_fractions))
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._announced_budget: str | None = None
        self._told: set[tuple[str, str]] = set()
        self.last_provider: str | None = None

    def google_budget_left(self) -> float | None:
        return None if self.usage is None else self.monthly_limit - self.usage.month_total("google_chars")

    def status(self) -> dict:
        """What the voice is doing: used/limit characters this month and which providers are paused (for the tray and tools)."""
        used = self.usage.month_total("google_chars") if self.usage is not None else 0
        now = self._clock()
        paused = {n: ("this session" if t == float("inf") else f"{int(t - now)} s") for n, t in self._down_until.items() if t > now}
        return {"used": int(used), "limit": int(self.monthly_limit),
                "fraction": used / self.monthly_limit if self.monthly_limit else 0.0, "paused": paused,
                "last_provider": self.last_provider}

    def _tell(self, kind: str, key: str, title: str, message: str) -> None:
        """One notification per (kind, key): a tray message the user can act on. Never raises."""
        if (kind, key) in self._told:
            return
        self._told.add((kind, key))
        log.warning("%s: %s", title, message)
        if self.notify is not None:
            try:
                self.notify(title, message)
            except Exception:  # noqa: BLE001
                log.debug("notify failed", exc_info=True)

    def _skip_reason(self, p: TTSProvider, text: str) -> str | None:
        if not p.available():
            return "not configured"
        if self._clock() < self._down_until.get(p.name, 0):
            return "paused after a failure"
        if p.name == "google" and self.usage is not None:
            used = self.usage.month_total("google_chars")
            if used >= self.monthly_limit * self.switch_fraction or used + len(text) > self.monthly_limit:
                month = self.usage._month()
                if self._announced_budget != month:
                    self._announced_budget = month
                    self._tell("limit", month, "Vex voice: free allowance nearly used",
                               f"Google voice has used {int(used):,} of {int(self.monthly_limit):,} free characters this month, so Vex "
                               "is using edge-tts until it resets.")
                return "monthly allowance"
        return None

    def _warn_if_near(self) -> None:
        """Tell the user once per threshold per month (75%, 90% by default) before the switch to the free voice."""
        if self.usage is None or not self.monthly_limit:
            return
        used = self.usage.month_total("google_chars")
        crossed = [f for f in self.warn_fractions if used >= self.monthly_limit * f]
        if not crossed:
            return
        f = crossed[-1]
        flag = f"tts_warned_{int(round(f * 100))}"
        if self.usage.month_total(flag):                       # already told this month (survives a restart)
            return
        self.usage.add_month(flag, 1)
        self._tell("warn", f"{self.usage._month()}-{f}", "Vex voice: free allowance",
                   f"Google voice has used {used / self.monthly_limit:.0%} of this month's free {int(self.monthly_limit):,} characters. "
                   f"At {self.switch_fraction:.0%} Vex switches to the free edge voice.")

    def _failed(self, p: TTSProvider, e: Exception) -> None:
        if isinstance(e, TTSQuotaError):
            self._down_until[p.name] = self._clock() + self.quota_pause
            self._tell("quota", p.name, f"Vex voice: {p.name} quota reached",
                       f"{p.name.capitalize()} voice hit its quota or billing limit. Using the next voice for the next hour.")
        elif isinstance(e, TTSAuthError):
            self._down_until[p.name] = float("inf")
            self._tell("auth", p.name, f"Vex voice: {p.name} was rejected",
                       f"{p.name.capitalize()} voice was rejected. Check the key, that the Text-to-Speech API is enabled and "
                       "billing is on. Using the free voice for now.")
        else:
            self._down_until[p.name] = self._clock() + self.retry_after
        log.warning("TTS %s failed (%s); trying the next provider", p.name, e)

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
                self._failed(p, e)
                continue
            if p.name == "google" and self.usage is not None:
                self.usage.add_month("google_chars", len(text))
                self._warn_if_near()
            self.last_provider = p.name
            return audio
        raise TTSUnavailable(str(last) if last else "no text-to-speech provider is available")
