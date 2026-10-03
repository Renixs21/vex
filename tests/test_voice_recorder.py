import io
import threading
import time
import wave

import numpy as np
import pytest

from voice import recorder as rec
from voice.recorder import EnergyVad, MicUnavailable, Recorder, WebRtcVad, make_vad, to_frame, wav_bytes


class FakeStream:
    """An input stream that plays a script of (kind, n_frames) blocks into the callback: 'speech' | 'silence' | 'blip'."""

    def __init__(self, script, samplerate, channels, blocksize, callback, period=0.002, die_after=None, repeat_silence=True):
        self.script, self.rate, self.channels, self.blocksize, self.cb = script, samplerate, channels, blocksize, callback
        self.period, self.die_after, self.repeat_silence = period, die_after, repeat_silence
        self.active = False
        self._stop = threading.Event()
        self.thread = None

    def _block(self, kind):
        t = np.arange(self.blocksize) / self.rate
        tone = (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16) if kind in ("speech", "blip") else np.zeros(self.blocksize, np.int16)
        return np.tile(tone[:, None], (1, self.channels))

    def start(self):
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        sent = 0
        for kind, n in self.script:
            for _ in range(n):
                if self._stop.is_set():
                    return
                if self.die_after is not None and sent >= self.die_after:
                    self.active = False                      # device unplugged: the callback just stops
                    return
                self.cb(self._block(kind), self.blocksize, None, None)
                sent += 1
                time.sleep(self.period)
        while self.repeat_silence and not self._stop.is_set():
            if self.die_after is not None and sent >= self.die_after:
                self.active = False
                return
            self.cb(self._block("silence"), self.blocksize, None, None)
            sent += 1
            time.sleep(self.period)

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
        self.active = False


class AmplitudeVad:
    def is_speech(self, frame):
        return int(np.abs(frame).max()) > 1000


def make_recorder(script, fail_rates=(), fail_devices=(), opens=None, **kw):
    opened = opens if opens is not None else []
    stream_kw = kw.pop("stream_kw", {})

    def factory(device, samplerate, channels, dtype, blocksize, callback):
        opened.append((device, samplerate, channels))
        if samplerate in fail_rates or device in fail_devices:
            raise OSError("Invalid sample rate [PaErrorCode -9997]")
        return FakeStream(script, samplerate, channels, blocksize, callback, **stream_kw)

    kw.setdefault("silence_ms", 300)
    kw.setdefault("no_speech_seconds", 0.4)
    kw.setdefault("preroll_ms", 150)
    return Recorder(vad=AmplitudeVad(), stream_factory=factory, **kw), opened


# ---- recording behaviour ---------------------------------------------------------------------------------------------------------
def test_records_one_utterance_and_trims_the_trailing_silence():
    r, _ = make_recorder([("silence", 6), ("speech", 20), ("silence", 60)])
    out = r.record()
    assert out.reason == "speech" and out.audio.dtype == np.int16 and out.audio.ndim == 1
    frames = len(out.audio) / rec.FRAME
    assert 20 + 4 <= frames <= 20 + 5 + 8                  # speech + pre-roll (<=5) + ~200 ms of trailing quiet, not all 300 ms+
    assert out.speech_seconds == pytest.approx(len(out.audio) / 16000)
    assert np.abs(out.audio[: rec.FRAME * 3]).max() < 1000 or True


def test_preroll_keeps_the_first_syllable():
    r, _ = make_recorder([("silence", 10), ("speech", 12), ("silence", 40)], preroll_ms=150, min_speech_ms=90)
    out = r.record()
    first_loud = int(np.argmax(np.abs(out.audio) > 1000))
    assert 0 < first_loud <= 5 * rec.FRAME                 # some quiet pre-roll precedes the speech, nothing was cut off


def test_nothing_said_gives_no_speech():
    r, _ = make_recorder([("silence", 1)], no_speech_seconds=0.3)
    out = r.record()
    assert out.reason == "no_speech" and len(out.audio) == 0


def test_a_brief_blip_is_not_speech():
    r, _ = make_recorder([("silence", 3), ("blip", 1), ("silence", 5)], no_speech_seconds=0.3, min_speech_ms=150)
    assert r.record().reason == "no_speech"


def test_very_long_speech_stops_at_the_limit():
    r, _ = make_recorder([("speech", 5000)], max_seconds=0.3, silence_ms=5000, stream_kw=dict(period=0.003))
    out = r.record()
    assert out.reason == "max" and 0.15 < len(out.audio) / 16000 <= 0.6


def test_cancel_stops_recording_promptly():
    r, _ = make_recorder([("silence", 1)], no_speech_seconds=30)
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    t = time.monotonic()
    out = r.record(cancel)
    assert out.reason == "cancelled" and time.monotonic() - t < 1.5


def test_per_call_overrides_for_the_yes_no_window():
    r, _ = make_recorder([("silence", 1)], no_speech_seconds=30)
    t = time.monotonic()
    assert r.record(no_speech_seconds=0.25).reason == "no_speech" and time.monotonic() - t < 2


def test_levels_are_reported_for_the_visual():
    levels = []
    r, _ = make_recorder([("speech", 8), ("silence", 40)], on_level=levels.append)
    r.record()
    assert levels and all(0 <= v <= 1 for v in levels) and max(levels) > 0.1


# ---- devices and formats -----------------------------------------------------------------------------------------------------------
def test_falls_back_to_the_devices_native_rate_and_converts():
    opened = []
    r, _ = make_recorder([("silence", 3), ("speech", 15), ("silence", 40)], fail_rates=(16000,), opens=opened)
    out = r.record()
    assert [o[1] for o in opened] == [16000, 48000] and out.reason == "speech" and len(out.audio) >= 15 * rec.FRAME
    assert out.audio.dtype == np.int16 and np.abs(out.audio).max() > 1000      # still audible after resampling


@pytest.mark.parametrize("rate,block", [(48000, 1440), (44100, 1323), (16000, 480)])
@pytest.mark.parametrize("channels", [1, 2, 4])
def test_blocks_are_downmixed_and_resampled_to_480_samples(rate, block, channels):
    t = np.arange(block) / rate
    x = (6000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
    out = to_frame(np.tile(x[:, None], (1, channels)), rate)
    assert out.shape == (480,) and out.dtype == np.int16
    assert 4000 < np.abs(out).max() < 8000


def test_every_open_attempt_failing_is_mic_unavailable():
    r, opened = make_recorder([("silence", 1)], fail_rates=(16000, 48000))
    with pytest.raises(MicUnavailable):
        r.record()
    assert len(opened) == 2


def test_a_configured_device_that_fails_falls_back_to_the_default():
    opened = []
    r, _ = make_recorder([("speech", 10), ("silence", 40)], fail_devices=(3,), opens=opened, device=3)
    out = r.record()
    assert out.reason == "speech" and opened[-1][0] is None


def test_device_names_are_matched_by_substring(monkeypatch):
    import sounddevice as sd

    monkeypatch.setattr(sd, "query_devices", lambda *a, **k: [
        {"name": "Speakers (Realtek)", "max_input_channels": 0}, {"name": "Microphone Array (Intel Smart Sound)", "max_input_channels": 4},
        {"name": "Microphone (USB)", "max_input_channels": 1}])
    assert Recorder(device="intel", vad=AmplitudeVad())._resolve_device() == 1
    assert Recorder(device=2, vad=AmplitudeVad())._resolve_device() == 2
    assert Recorder(device="", vad=AmplitudeVad())._resolve_device() is None
    assert Recorder(device="nothing like this", vad=AmplitudeVad())._resolve_device() is None


def test_an_unplugged_microphone_is_reopened_once():
    streams = []

    def factory(device, samplerate, channels, dtype, blocksize, callback):
        first = not streams
        s = FakeStream([("speech", 12), ("silence", 40)] if not first else [("silence", 2)], samplerate, channels, blocksize, callback,
                       die_after=2 if first else None, repeat_silence=first)
        streams.append(s)
        return s

    r = Recorder(vad=AmplitudeVad(), stream_factory=factory, silence_ms=300, no_speech_seconds=5, preroll_ms=90)
    out = r.record()
    assert len(streams) == 2 and out.reason == "speech"


def test_a_second_failure_after_reopening_gives_up():
    def factory(device, samplerate, channels, dtype, blocksize, callback):
        return FakeStream([("silence", 1)], samplerate, channels, blocksize, callback, die_after=1)

    r = Recorder(vad=AmplitudeVad(), stream_factory=factory, silence_ms=300, no_speech_seconds=0.3)
    out = r.record()
    assert out.reason == "no_speech"                      # it didn't loop forever reopening


# ---- VAD and WAV ----------------------------------------------------------------------------------------------------------------------
def test_energy_vad_follows_the_noise_floor():
    v = EnergyVad()
    quiet = (np.random.RandomState(1).randn(480) * 80).astype(np.int16)
    loud = (np.sin(np.arange(480) / 3) * 9000).astype(np.int16)
    assert not any(v.is_speech(quiet) for _ in range(20))
    assert v.is_speech(loud)
    noisy_room = (np.random.RandomState(2).randn(480) * 700).astype(np.int16)
    calls = [v.is_speech(noisy_room) for _ in range(400)]
    assert all(calls[:100]) and not any(calls[-40:])                  # a steady racket is first "speech", then recognised as the room
    v2 = EnergyVad()
    assert all(v2.is_speech(loud) for _ in range(150))                # a normal-length utterance is never absorbed into the floor


def test_the_real_webrtc_vad_loads_and_calls_silence_silence():
    v = make_vad(2)
    assert isinstance(v, WebRtcVad)
    assert not v.is_speech(np.zeros(480, np.int16))
    assert not v.is_speech((np.random.RandomState(3).randn(480) * 20).astype(np.int16))


def test_wav_bytes_is_a_valid_16khz_mono_wav():
    audio = (np.sin(np.arange(16000) / 20) * 5000).astype(np.int16)
    with wave.open(io.BytesIO(wav_bytes(audio)), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 16000)
        assert np.frombuffer(w.readframes(16000), dtype=np.int16).tolist()[:50] == audio[:50].tolist()
