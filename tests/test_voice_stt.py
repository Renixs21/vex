import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from voice.stt import GroqWhisper, SttLimitReached, SttUnavailable
from voice.usage import UsageStore

AUDIO = (np.sin(np.arange(16000) / 12) * 6000).astype(np.int16)          # one second


class ApiError(Exception):
    def __init__(self, code, headers=None, message=None):
        super().__init__(message or f"HTTP {code}")
        self.status_code = code
        if headers is not None:
            self.response = SimpleNamespace(headers=headers)


class FakeGroq:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if self.replies else SimpleNamespace(text="open notepad", segments=[])
        if isinstance(r, Exception):
            raise r
        return r


def reply(text, no_speech=0.01):
    return SimpleNamespace(text=text, segments=[{"no_speech_prob": no_speech, "avg_logprob": -0.2}])


class Clock:
    def __init__(self):
        self.t = 100000.0

    def __call__(self):
        return self.t


def make(tmp_path, *replies, **kw):
    clock = kw.pop("clock", None) or Clock()
    usage = UsageStore(tmp_path / "usage.json", clock=clock)
    client = FakeGroq(*replies)
    return GroqWhisper(model="whisper-large-v3-turbo", language="en", prompt="Vex", usage=usage, client=client, clock=clock, **kw), client, usage, clock


def test_request_shape_and_text(tmp_path):
    stt, client, usage, _ = make(tmp_path, reply("Open Notepad."))
    assert stt.transcribe(AUDIO) == "Open Notepad."
    kw = client.calls[0]
    assert kw["model"] == "whisper-large-v3-turbo" and kw["language"] == "en" and kw["prompt"] == "Vex"
    assert kw["response_format"] == "verbose_json" and kw["temperature"] == 0.0
    name, data = kw["file"]
    assert name == "speech.wav"
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 16000)


def test_language_can_be_auto_detected(tmp_path):
    stt, client, *_ = make(tmp_path, reply("namaste"))
    stt.language = ""
    stt.transcribe(AUDIO)
    assert "language" not in client.calls[0]


def test_very_short_audio_is_not_sent(tmp_path):
    stt, client, usage, _ = make(tmp_path)
    assert stt.transcribe(AUDIO[:1000]) == "" and client.calls == [] and usage.window_total("whisper", 3600) == 0


def test_seconds_are_counted(tmp_path):
    stt, client, usage, _ = make(tmp_path, reply("a b"), reply("c d"))
    stt.transcribe(AUDIO)
    stt.transcribe(AUDIO[:8000].copy().repeat(1))
    assert usage.window_total("whisper", 3600) == pytest.approx(1.5)
    assert stt.remaining_seconds() == pytest.approx(7200 - 1.5)


def test_the_local_hourly_allowance_fails_to_typing_before_calling_groq(tmp_path):
    stt, client, usage, clock = make(tmp_path, seconds_per_hour=3.0)
    usage.add_event("whisper", 2.5)
    clock.t += 600
    with pytest.raises(SttLimitReached) as e:
        stt.transcribe(AUDIO)                                         # 2.5 + 1 > 3
    assert client.calls == [] and 2900 < e.value.wait <= 3000          # wait until the first usage ages out of the hour
    clock.t += 3000
    assert stt.transcribe(AUDIO) == "open notepad" and len(client.calls) == 1


def test_groq_429_blocks_further_calls_for_the_stated_time(tmp_path):
    stt, client, usage, clock = make(tmp_path, ApiError(429, headers={"retry-after": "120"}))
    with pytest.raises(SttLimitReached) as e:
        stt.transcribe(AUDIO)
    assert e.value.wait == 120 and len(client.calls) == 1
    clock.t += 30
    with pytest.raises(SttLimitReached) as e2:
        stt.transcribe(AUDIO)
    assert len(client.calls) == 1 and 85 < e2.value.wait < 95            # no second request while blocked
    clock.t += 100
    assert stt.transcribe(AUDIO) == "open notepad"
    assert usage.window_total("whisper", 3600) == 1.0                    # the failed request wasn't counted


def test_a_server_error_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setattr("voice.stt.time.sleep", lambda s: None)
    stt, client, *_ = make(tmp_path, ApiError(503), reply("close notepad"))
    assert stt.transcribe(AUDIO) == "close notepad" and len(client.calls) == 2


def test_persistent_failures_become_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("voice.stt.time.sleep", lambda s: None)
    stt, client, *_ = make(tmp_path, ApiError(503), ApiError(503))
    with pytest.raises(SttUnavailable):
        stt.transcribe(AUDIO)

    class ConnectError(Exception):
        pass

    stt2, _c, *_ = make(tmp_path, ConnectError("offline"), ConnectError("offline"))
    with pytest.raises(SttUnavailable):
        stt2.transcribe(AUDIO)
    stt3, c3, *_ = make(tmp_path, ApiError(401))
    with pytest.raises(SttUnavailable):
        stt3.transcribe(AUDIO)
    assert len(c3.calls) == 1                                           # a bad key isn't retried


def test_no_key_means_unavailable_without_a_request():
    stt = GroqWhisper(api_key="")
    assert not stt.available
    with pytest.raises(SttUnavailable):
        stt.transcribe(AUDIO)


@pytest.mark.parametrize("text,no_speech,seconds_ok", [
    ("Thank you.", 0.5, True), ("Thanks for watching!", 0.4, True), ("you", 0.35, True), ("Bye.", 0.9, True),
    ("Open Notepad.", 0.9, True),            # Whisper itself says there was no speech
])
def test_silence_hallucinations_are_dropped(tmp_path, text, no_speech, seconds_ok):
    stt, *_ = make(tmp_path, reply(text, no_speech))
    assert stt.transcribe(AUDIO) == ""


@pytest.mark.parametrize("text", ["Thank you for opening Notepad.", "Open notepad", "Yes", "No", "Cancel", "What time is it?"])
def test_real_commands_are_kept(tmp_path, text):
    stt, *_ = make(tmp_path, reply(text, 0.02))
    assert stt.transcribe(AUDIO) == text


def test_a_clear_thank_you_after_a_real_utterance_is_kept(tmp_path):
    stt, *_ = make(tmp_path, reply("Thank you.", 0.01))
    assert stt.transcribe(np.tile(AUDIO, 3)) == "Thank you."             # 3 s long, confident: a real "thank you"


def test_empty_text_is_empty(tmp_path):
    stt, *_ = make(tmp_path, SimpleNamespace(text="  ", segments=[]))
    assert stt.transcribe(AUDIO) == ""
