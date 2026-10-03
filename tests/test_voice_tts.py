import io
import logging
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from voice.tts import Audio, EdgeTTS, GoogleTTS, TTSChain, TTSError, TTSUnavailable, decode_audio
from voice.usage import UsageStore


def tone(seconds=0.5, rate=24000):
    return (0.3 * np.sin(np.arange(int(seconds * rate)) / 20)).astype(np.float32)


def encoded(fmt, seconds=0.5, rate=24000, subtype=None):
    buf = io.BytesIO()
    sf.write(buf, tone(seconds, rate), rate, format=fmt, subtype=subtype)
    return buf.getvalue()


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- decoding ------------------------------------------------------------------------------------------------------------------
def test_mp3_and_wav_decode_to_mono_float():
    for fmt, sub in (("MP3", None), ("WAV", "PCM_16")):
        a = decode_audio(encoded(fmt, subtype=sub))
        assert a.samples.dtype == np.float32 and a.samples.ndim == 1 and a.rate == 24000
        assert 0.4 < len(a.samples) / a.rate < 0.7 and np.abs(a.samples).max() > 0.1


def test_stereo_is_mixed_down_and_empty_audio_is_an_error():
    buf = io.BytesIO()
    sf.write(buf, np.stack([tone(), tone()], axis=1), 24000, format="WAV")
    assert decode_audio(buf.getvalue()).samples.ndim == 1
    with pytest.raises(Exception):
        decode_audio(b"not audio at all")


# ---- edge-tts ---------------------------------------------------------------------------------------------------------------------
class FakeComm:
    def __init__(self, text, voice, data=None, fail=None):
        self.text, self.voice, self.data, self.fail = text, voice, data, fail

    async def stream(self):
        if self.fail:
            raise self.fail
        yield {"type": "WordBoundary", "text": "x"}
        half = len(self.data) // 2
        yield {"type": "audio", "data": self.data[:half]}
        yield {"type": "audio", "data": self.data[half:]}


def test_edge_tts_assembles_the_stream_and_decodes():
    seen = []

    def factory(text, voice):
        seen.append((text, voice))
        return FakeComm(text, voice, data=encoded("MP3"))

    audio = EdgeTTS("en-IN-NeerjaNeural", communicate_factory=factory).synthesize("Volume set.")
    assert seen == [("Volume set.", "en-IN-NeerjaNeural")] and audio.rate == 24000 and len(audio.samples) > 5000


def test_edge_tts_errors_become_tts_errors():
    with pytest.raises(TTSError, match="edge-tts failed"):
        EdgeTTS(communicate_factory=lambda t, v: FakeComm(t, v, fail=ConnectionError("offline"))).synthesize("hi")


def test_the_real_edge_tts_package_is_wired_correctly():
    import edge_tts

    assert hasattr(edge_tts.Communicate, "stream")


# ---- Google (fake client: the real key comes later) ----------------------------------------------------------------------------
class FakeGoogleClient:
    def __init__(self, voices=("en-US-Chirp3-HD-Aoede", "en-US-Chirp3-HD-Puck", "en-US-Wavenet-A"), fail=None):
        self.voices, self.fail, self.calls, self.list_calls = voices, fail, [], []

    def list_voices(self, language_code):
        self.list_calls.append(language_code)
        return SimpleNamespace(voices=[SimpleNamespace(name=n) for n in self.voices])

    def synthesize_speech(self, input, voice, audio_config):  # noqa: A002 - the real API's argument names
        self.calls.append((input, voice, audio_config))
        if self.fail:
            raise self.fail
        return SimpleNamespace(audio_content=encoded("WAV", subtype="PCM_16"))


def test_google_request_is_chirp3_hd_linear16_24k():
    c = FakeGoogleClient()
    audio = GoogleTTS("en-US-Chirp3-HD-Aoede", "en-US", client=c).synthesize("Hello there.")
    inp, voice, cfg = c.calls[0]
    assert inp.text == "Hello there." and voice.name == "en-US-Chirp3-HD-Aoede" and voice.language_code == "en-US"
    assert cfg.sample_rate_hertz == 24000 and cfg.audio_encoding.name == "LINEAR16"
    assert audio.rate == 24000 and len(audio.samples) > 5000


def test_google_checks_the_voice_name_once():
    c = FakeGoogleClient()
    g = GoogleTTS("en-US-Chirp3-HD-Puck", client=c)
    g.synthesize("a")
    g.synthesize("b")
    assert c.list_calls == ["en-US"]
    bad = GoogleTTS("en-US-Chirp3-HD-Nope", client=FakeGoogleClient())
    with pytest.raises(TTSError, match="not found.*Aoede"):
        bad.synthesize("x")


def test_google_failures_are_tts_errors():
    with pytest.raises(TTSError, match="Google TTS failed"):
        GoogleTTS(client=FakeGoogleClient(fail=PermissionError("billing disabled"))).synthesize("x")


def test_google_availability_follows_the_key_file(tmp_path):
    assert not GoogleTTS(credentials=tmp_path / "missing.json").available()
    key = tmp_path / "key.json"
    key.write_text("{}")
    assert GoogleTTS(credentials=key).available() and GoogleTTS(client=object()).available() and not GoogleTTS().available()


# ---- the chain ----------------------------------------------------------------------------------------------------------------------
class Fake:
    def __init__(self, name, fail=None, available=True):
        self.name, self.fail, self._avail, self.calls = name, fail, available, []

    def available(self):
        return self._avail

    def synthesize(self, text):
        self.calls.append(text)
        if self.fail:
            raise TTSError(self.fail)
        return Audio(tone(0.1), 24000)


def chain(tmp_path, providers, **kw):
    clock = Clock()
    usage = UsageStore(tmp_path / "u.json", clock=clock)
    return TTSChain(providers, usage, clock=clock, **kw), usage, clock


def test_google_is_used_first_and_its_characters_are_counted(tmp_path):
    g, e = Fake("google"), Fake("edge")
    c, usage, _ = chain(tmp_path, [g, e])
    c.synthesize("Hello there.")
    c.synthesize("Again.")
    assert g.calls == ["Hello there.", "Again."] and e.calls == [] and c.last_provider == "google"
    assert usage.month_total("google_chars") == len("Hello there.") + len("Again.")


def test_without_a_key_edge_is_used_and_nothing_is_counted(tmp_path):
    g, e = Fake("google", available=False), Fake("edge")
    c, usage, _ = chain(tmp_path, [g, e])
    c.synthesize("hi")
    assert g.calls == [] and e.calls == ["hi"] and c.last_provider == "edge" and usage.month_total("google_chars") == 0


def test_a_google_error_falls_back_to_edge_and_is_not_retried_for_a_minute(tmp_path):
    g, e = Fake("google", fail="quota"), Fake("edge")
    c, usage, clock = chain(tmp_path, [g, e])
    c.synthesize("one")
    c.synthesize("two")
    assert g.calls == ["one"] and e.calls == ["one", "two"]             # the second sentence didn't wait for Google to fail again
    assert usage.month_total("google_chars") == 0                        # failures aren't billed to the counter
    clock.t += 61
    c.synthesize("three")
    assert g.calls == ["one", "three"]


def test_near_the_free_allowance_it_switches_to_edge_and_says_so_once(tmp_path, caplog):
    g, e = Fake("google"), Fake("edge")
    c, usage, _ = chain(tmp_path, [g, e], monthly_limit=1000, switch_fraction=0.9)
    usage.add_month("google_chars", 880)
    c.synthesize("x" * 15)                                                # 895 of 1000
    assert g.calls and not e.calls
    with caplog.at_level(logging.WARNING, logger="vex.tts"):
        c.synthesize("y" * 20)                                            # 895 is still under 90%: google, which brings it to 915
        c.synthesize("z")                                                 # now past 90%: edge, and the log says so
        c.synthesize("w")
    assert e.calls == ["z", "w"] and caplog.text.count("using edge-tts until it resets") == 1      # announced once, not per sentence


def test_text_that_would_exceed_the_hard_limit_goes_to_edge(tmp_path):
    g, e = Fake("google"), Fake("edge")
    c, usage, _ = chain(tmp_path, [g, e], monthly_limit=100, switch_fraction=1.0)
    usage.add_month("google_chars", 80)
    c.synthesize("a" * 30)
    assert g.calls == [] and len(e.calls) == 1


def test_a_new_month_starts_a_fresh_allowance(tmp_path):
    g, e = Fake("google"), Fake("edge")
    c, usage, clock = chain(tmp_path, [g, e], monthly_limit=1000)
    usage.add_month("google_chars", 990)
    c.synthesize("a")
    assert not g.calls
    clock.t += 32 * 86400
    c.synthesize("b")
    assert g.calls == ["b"] and usage.month_total("google_chars") == 1


def test_every_provider_failing_raises_tts_unavailable(tmp_path):
    c, *_ = chain(tmp_path, [Fake("google", fail="a"), Fake("edge", fail="offline")])
    with pytest.raises(TTSUnavailable, match="offline"):
        c.synthesize("hi")
    c2, *_ = chain(tmp_path, [Fake("google", available=False)])
    with pytest.raises(TTSUnavailable):
        c2.synthesize("hi")


def test_usage_survives_a_restart(tmp_path):
    clock = Clock()
    UsageStore(tmp_path / "u.json", clock=clock).add_month("google_chars", 123)
    assert UsageStore(tmp_path / "u.json", clock=clock).month_total("google_chars") == 123
    assert "123" in (tmp_path / "u.json").read_text()
    assert "text" not in (tmp_path / "u.json").read_text().lower().replace("months", "")        # counts only, never content
