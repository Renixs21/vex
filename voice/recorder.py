"""Microphone recorder: starts when you trigger it, ends a moment after you stop talking (`silence_ms`, 0.7 s by default).

  * Mid-sentence-pause safeguard: when it ends on silence it keeps the microphone open for `grace_ms` more (in the background, while the
    speech recognition is already running). If you carry on talking in that window the recording is extended: `Recording.resume.wait()`
    returns the extra audio and the caller transcribes the two parts together. A pause longer than the window still ends the turn.

  * 16 kHz mono 16-bit in 30 ms frames (what the VAD and Whisper want), whatever the microphone delivers (the laptop mic array
    is 4-channel at 44.1/48 kHz: it is down-mixed and resampled here).
  * Voice activity detection with webrtcvad, or an adaptive energy gate if it can't be loaded.
  * A 0.5 s pre-roll so the first syllable isn't clipped, a no-speech timeout, a maximum length.
  * If the device is unplugged or changes, the stream is reopened on the current default once before giving up.
Audio stays in memory: nothing is written to disk or logged.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
from math import gcd
from scipy.signal import resample_poly              # imported up front: a lazy import would delay the first recording

log = logging.getLogger("vex.recorder")

RATE = 16000
FRAME_MS = 30
FRAME = RATE * FRAME_MS // 1000          # 480 samples


NEAR_SILENT_RMS = 20.0               # a recording whose loudest frame is below this almost certainly means a muted/dead microphone


def rms_to_level(rms: float) -> float:
    """Loudness 0..1 for the visual: gentle speech (RMS ~300) is already clearly visible, loud speech (~2500+) fills it."""
    return min(1.0, (max(rms, 0.0) / 2500.0) ** 0.6)


class MicUnavailable(Exception):
    """No usable microphone (missing, busy, or blocked by Windows privacy settings)."""


@dataclass
class Recording:
    audio: np.ndarray                    # int16 mono at 16 kHz (empty if nothing was recorded)
    reason: str                          # speech | no_speech | max | cancelled
    seconds: float = 0.0
    speech_seconds: float = 0.0
    peak_rms: float = 0.0                # loudest frame heard (int16 units): ~1 in a silent room, 300-4000 for speech
    t_start: float = 0.0                 # time.monotonic() when the microphone was listening, when the last voiced frame was heard,
    t_last_voiced: float = 0.0           # and when the recorder decided you were done (for the latency timeline)
    t_end: float = 0.0
    resume: "ResumeProbe | None" = None  # the safeguard: speech that carries on right after the endpoint (see ResumeProbe)


class ResumeProbe:
    """Keeps listening for `grace_ms` after the recorder ended on silence, in a background thread.

    `wait(timeout)` returns None if you did not speak again, or the extra audio (int16, 16 kHz) once the continuation is over
    (it ends on the same silence rule, up to `max_extra_s`). The thread closes the microphone stream when it is done."""

    def __init__(self, stream, frames: "queue.Queue[np.ndarray]", vad, close: Callable, grace_ms: int, resume_ms: int, silence_ms: int,
                 cancel: threading.Event | None, max_extra_s: float = 8.0):
        self._stream, self._frames, self._vad, self._close = stream, frames, vad, close
        self.grace_ms, self.resume_ms, self.silence_ms = grace_ms, resume_ms, silence_ms
        self._cancel = cancel
        self.max_extra_s = max_extra_s
        self.resumed = False
        self.audio: np.ndarray | None = None
        self.gap_s: float | None = None                 # silence between the endpoint and the first resumed frame
        self._done = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="resume-probe", daemon=True)

    def start(self) -> "ResumeProbe":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float = 3.0) -> np.ndarray | None:
        self._done.wait(timeout)
        return self.audio if (self._done.is_set() and self.resumed) else None

    def _cancelled(self) -> bool:
        return self._stop.is_set() or (self._cancel is not None and self._cancel.is_set())

    def _run(self) -> None:
        t0 = time.monotonic()
        recent: deque[np.ndarray] = deque(maxlen=4)
        extra: list[np.ndarray] = []
        voiced = silence = 0
        started = False
        try:
            while not self._cancelled():
                try:
                    frame = self._frames.get(timeout=0.1)
                except queue.Empty:
                    if not started and (time.monotonic() - t0) * 1000 >= self.grace_ms:
                        break
                    continue
                is_speech = self._vad.is_speech(frame)
                if not started:
                    recent.append(frame)
                    voiced = voiced + 1 if is_speech else 0
                    if voiced * FRAME_MS >= self.resume_ms:
                        started = True
                        self.resumed = True
                        self.gap_s = round(time.monotonic() - t0 - voiced * FRAME_MS / 1000, 3)
                        extra = list(recent)
                        silence = 0
                    elif (time.monotonic() - t0) * 1000 >= self.grace_ms:
                        break
                else:
                    extra.append(frame)
                    silence = 0 if is_speech else silence + 1
                    if silence * FRAME_MS >= self.silence_ms or len(extra) * FRAME_MS / 1000 >= self.max_extra_s:
                        break
        except Exception:  # noqa: BLE001 - never leave the caller waiting
            log.exception("resume probe failed")
        finally:
            if started and extra:
                keep = max(0, len(extra) - max(0, silence - 7))
                self.audio = np.concatenate(extra[:keep]) if keep else None
                if self.audio is None:
                    self.resumed = False
            else:
                self.resumed = False
            self._close(self._stream)
            self._done.set()


# ---- voice activity detection ------------------------------------------------------------------------------------------
class WebRtcVad:
    def __init__(self, aggressiveness: int = 2):
        import webrtcvad

        self._vad = webrtcvad.Vad(max(0, min(3, int(aggressiveness))))

    def is_speech(self, frame: np.ndarray) -> bool:
        return self._vad.is_speech(frame.tobytes(), RATE)


class EnergyVad:
    """Fallback: speech if the frame is clearly louder than the running noise floor."""

    def __init__(self, ratio: float = 3.0, floor: float = 250.0):
        self.ratio = ratio
        self.noise = floor
        self.min_level = floor
        self._loud_run = 0

    def is_speech(self, frame: np.ndarray) -> bool:
        level = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        speech = level > max(self.noise * self.ratio, self.min_level * 1.5)
        if not speech:
            self._loud_run = 0
            self.noise = 0.95 * self.noise + 0.05 * max(level, 1.0)       # follow the room's noise floor while quiet
        else:
            self._loud_run += 1
            if self._loud_run > 160:                                      # ~5 s of "speech" that never lets up is the room
                self.noise = 0.9 * self.noise + 0.1 * level
        return speech


def make_vad(aggressiveness: int = 2):
    try:
        return WebRtcVad(aggressiveness)
    except Exception:  # noqa: BLE001 - missing wheel or import error
        log.warning("webrtcvad unavailable; using the energy-based voice detector")
        return EnergyVad()


def wav_bytes(audio: np.ndarray, rate: int = RATE) -> bytes:
    """16-bit mono WAV in memory (what Whisper receives)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(audio, dtype=np.int16).tobytes())
    return buf.getvalue()


def to_frame(block: np.ndarray, rate_in: int) -> np.ndarray:
    """One audio block (samples x channels, any dtype/rate) -> 480 int16 mono samples at 16 kHz."""
    x = np.asarray(block)
    if x.ndim > 1:
        x = x.astype(np.float32).mean(axis=1)
    x = x.astype(np.float32)
    if rate_in != RATE:
        g = gcd(RATE, rate_in)
        x = resample_poly(x, RATE // g, rate_in // g)
    if len(x) < FRAME:
        x = np.pad(x, (0, FRAME - len(x)))
    return np.clip(x[:FRAME], -32768, 32767).astype(np.int16)


class Recorder:
    def __init__(self, device: str | int | None = "", silence_ms: int = 1000, max_seconds: float = 20,
                 no_speech_seconds: float = 6, preroll_ms: int = 500, min_speech_ms: int = 150, vad=None,
                 stream_factory: Callable | None = None, on_level: Callable[[float], None] | None = None,
                 vad_aggressiveness: int = 2, grace_ms: int = 0, resume_ms: int = 120):
        self.device = device or None
        self.silence_ms = silence_ms
        self.grace_ms = grace_ms              # 0 = no mid-sentence-pause safeguard
        self.resume_ms = resume_ms            # this much speech inside the grace window counts as "you carried on"
        self.max_seconds = max_seconds
        self.no_speech_seconds = no_speech_seconds
        self.preroll_ms = preroll_ms
        self.min_speech_ms = min_speech_ms
        self.vad = vad if vad is not None else make_vad(vad_aggressiveness)
        self._factory = stream_factory
        self.on_level = on_level
        self._peak = 0.0
        self.seed = None      # one-shot: audio (16 kHz int16), or a function returning it, treated as the first thing heard (the wake word's post-wake buffer)

    # ---- device ----------------------------------------------------------------------------------------------------------
    def _resolve_device(self):
        """A device index: given as a number, or the first input whose name contains the configured text; else default."""
        if self.device is None or isinstance(self.device, int):
            return self.device
        import sounddevice as sd

        want = str(self.device).casefold()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and want in d["name"].casefold():
                return i
        log.warning("input device %r not found; using the default microphone", self.device)
        return None

    def resolve_device(self):
        """Public form of the device lookup (the wake-word listener opens its stream the same way)."""
        return self._resolve_device()

    def open_stream(self, device, callback):
        """Public form of the stream opener: (stream, rate). Raises MicUnavailable."""
        return self._open(device, callback)

    def _open(self, device, callback):
        """Open an input stream, trying 16 kHz mono first, then the device's own rate. Returns (stream, rate)."""
        import sounddevice as sd

        factory = self._factory or sd.InputStream
        info = None
        try:
            info = sd.query_devices(device, "input") if self._factory is None else None
        except Exception as e:  # noqa: BLE001
            raise MicUnavailable(f"no input device: {e}") from e
        native = int(info["default_samplerate"]) if info else 48000
        attempts = [(RATE, 1), (native, 1)]
        if info and info["max_input_channels"] > 1:
            attempts.append((native, int(info["max_input_channels"])))
        last: Exception | None = None
        for rate, channels in attempts:
            try:
                stream = factory(device=device, samplerate=rate, channels=channels, dtype="int16",
                                 blocksize=rate * FRAME_MS // 1000, callback=callback)
                stream.start()
                return stream, rate
            except Exception as e:  # noqa: BLE001 - PortAudioError, privacy block, device busy
                last = e
        raise MicUnavailable(str(last))

    # ---- recording -------------------------------------------------------------------------------------------------------
    def record(self, cancel: threading.Event | None = None, no_speech_seconds: float | None = None,
               max_seconds: float | None = None) -> Recording:
        """Block until one utterance has been captured. Raises MicUnavailable if the microphone can't be used."""
        self._peak = 0.0
        log.info("recording started (device %s)", self.device if self.device is not None else "default")
        try:
            out = self._record(cancel, no_speech_seconds, max_seconds)
        except MicUnavailable as e:
            log.warning("recording failed: %s", e)
            raise
        out.peak_rms = self._peak
        out.t_end = time.monotonic()
        log.info("recording ended: %.1fs total, %.1fs of speech, peak RMS %.0f (%s)", out.seconds, out.speech_seconds, out.peak_rms, out.reason)
        return out

    def _record(self, cancel, no_speech_seconds, max_seconds) -> Recording:
        no_speech = self.no_speech_seconds if no_speech_seconds is None else no_speech_seconds
        limit = self.max_seconds if max_seconds is None else max_seconds
        frames: "queue.Queue[np.ndarray]" = queue.Queue()
        rate_holder = {"rate": RATE}

        def callback(indata, n, time_info, status):  # noqa: ARG001 - PortAudio signature
            try:
                frames.put_nowait(to_frame(indata, rate_holder["rate"]))
            except Exception:  # noqa: BLE001 - never raise into the audio thread
                pass

        seed, self.seed = self.seed, None
        if callable(seed):                                        # fetched now, at the last moment, so it covers the chime and start-up gap
            seed = seed()
        if seed is not None and len(seed) >= FRAME:
            for i in range(0, len(seed) - FRAME + 1, FRAME):
                frames.put_nowait(seed[i:i + FRAME])
            log.info("recorder seeded with %.2fs of audio heard right after the wake word", len(seed) / RATE)
        device = self._resolve_device()
        stream, rate = self._open_with_retry(device, callback, rate_holder)
        preroll: deque[np.ndarray] = deque(maxlen=max(1, self.preroll_ms // FRAME_MS))
        speech: list[np.ndarray] = []
        started = False
        voiced = silence = 0
        n_frames = 0
        t0 = time.monotonic()
        last_voiced = t0
        reopened = False
        handed_off = False                                         # the resume probe takes over the stream after an endpoint on silence
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return Recording(np.zeros(0, np.int16), "cancelled", time.monotonic() - t0)
                try:
                    frame = frames.get(timeout=0.4)
                except queue.Empty:
                    if not getattr(stream, "active", True) and not reopened:      # unplugged / default device changed
                        log.warning("microphone stream stopped; reopening once")
                        reopened = True
                        self._close(stream)
                        stream, rate = self._open_with_retry(None, callback, rate_holder)
                    elif time.monotonic() - t0 > no_speech + 2 and not started:
                        return Recording(np.zeros(0, np.int16), "no_speech", time.monotonic() - t0)
                    continue
                n_frames += 1
                elapsed = max(time.monotonic() - t0, n_frames * FRAME_MS / 1000)     # limits count audio, even if frames arrive in a burst
                rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                self._peak = max(self._peak, rms)
                if self.on_level:
                    self.on_level(rms_to_level(rms))
                is_speech = self.vad.is_speech(frame)
                if is_speech:
                    last_voiced = time.monotonic()
                if not started:
                    preroll.append(frame)
                    voiced = voiced + 1 if is_speech else 0
                    if voiced * FRAME_MS >= self.min_speech_ms:
                        started = True
                        speech = list(preroll)
                        silence = 0
                    elif elapsed >= no_speech:
                        return Recording(np.zeros(0, np.int16), "no_speech", elapsed)
                else:
                    speech.append(frame)
                    silence = 0 if is_speech else silence + 1
                    if silence * FRAME_MS >= self.silence_ms:
                        rec = self._finish(speech, silence, "speech", elapsed)
                        rec.t_start, rec.t_last_voiced = t0, last_voiced
                        if self.grace_ms > 0 and len(rec.audio):
                            rec.resume = ResumeProbe(stream, frames, self.vad, self._close, self.grace_ms, self.resume_ms,
                                                     self.silence_ms, cancel).start()
                            handed_off = True
                        return rec
                    if elapsed >= limit:
                        rec = self._finish(speech, silence, "max", elapsed)
                        rec.t_start, rec.t_last_voiced = t0, last_voiced
                        return rec
        finally:
            if not handed_off:
                self._close(stream)

    def _open_with_retry(self, device, callback, rate_holder):
        try:
            stream, rate = self._open(device, callback)
        except MicUnavailable:
            if device is None:
                raise
            log.warning("configured microphone failed; trying the default one")
            stream, rate = self._open(None, callback)
        rate_holder["rate"] = rate
        log.info("microphone opened at %d Hz", rate)
        return stream, rate

    @staticmethod
    def _close(stream) -> None:
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _finish(speech: list[np.ndarray], trailing: int, reason: str, elapsed: float) -> Recording:
        keep = max(0, len(speech) - max(0, trailing - 7))               # trim the silence, leave ~200 ms
        audio = np.concatenate(speech[:keep]) if keep else np.zeros(0, np.int16)
        return Recording(audio, reason, elapsed, speech_seconds=len(audio) / RATE)
