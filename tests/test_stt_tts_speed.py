"""STT deadline + retry + backup chain, the warm edge-tts connection (never a dependency), and the report's new STT fields."""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from voice import stt as stt_mod
from voice.edge_warm import WarmEdgeTTS
from voice.stt import GeminiSTT, GroqWhisper, SttChain, SttLimitReached, SttUnavailable
from voice.timeline import Timeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
import latency_report  # noqa: E402

AUDIO = np.ones(16000, np.int16) * 800


class TimeoutError_(Exception):
    pass


class FakeClient:
    """groq-shaped client: audio.transcriptions.create(**kw) answers from a script of results/exceptions."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        outer = self

        class T:
            def create(self, **kw):
                outer.calls.append(kw)
                r = outer.script.pop(0)
                if isinstance(r, Exception):
                    raise r
                return {"text": r, "segments": []}

        class A:
            transcriptions = T()

        self.audio = A()


def groq(script, **kw):
    return GroqWhisper(client=FakeClient(script), **kw)


def test_groq_gets_a_short_deadline_scaled_by_audio_length():
    g = groq(["hello there"], timeout=3.0, timeout_per_second=0.5)
    assert g.transcribe(AUDIO) == "hello there"
    assert g.client.calls[0]["timeout"] == pytest.approx(3.5)


def test_one_retry_on_a_timeout_then_success_and_the_retry_is_recorded(monkeypatch):
    tl = Timeline(time.monotonic)
    monkeypatch.setattr(stt_mod, "timeline", tl)
    tl.begin("hotkey")
    g = groq([TimeoutError_("t"), "open notepad"])
    assert g.transcribe(AUDIO) == "open notepad" and len(g.client.calls) == 2 and tl._turn["data"]["stt_attempts"] == 2


def test_two_timeouts_raise_unavailable_and_retries_are_capped():
    g = groq([TimeoutError_(), TimeoutError_(), "never"], retries=1)
    with pytest.raises(SttUnavailable):
        g.transcribe(AUDIO)
    assert len(g.client.calls) == 2


def test_a_429_is_not_retried_but_reported_as_a_limit():
    e = Exception("rate")
    e.status_code = 429
    g = groq([e, "x"])
    with pytest.raises(SttLimitReached):
        g.transcribe(AUDIO)
    assert len(g.client.calls) == 1


class P:
    def __init__(self, name, result):
        self.name, self.result, self.available, self.model, self.calls = name, result, True, name + "-m", 0

    def transcribe(self, audio, rate=16000):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_the_chain_hands_the_same_audio_to_the_backup(monkeypatch):
    tl = Timeline(time.monotonic)
    monkeypatch.setattr(stt_mod, "timeline", tl)
    tl.begin("hotkey")
    a, b = P("groq", SttUnavailable("slow")), P("gemini", "play jazz")
    assert SttChain([a, b]).transcribe(AUDIO) == "play jazz"
    assert (a.calls, b.calls) == (1, 1) and tl._turn["data"]["stt_provider"] == "gemini" and tl._turn["data"]["stt_fallback"] is True


def test_the_chain_prefers_the_first_provider_and_surfaces_a_limit_if_everything_fails():
    a, b = P("groq", "hi"), P("gemini", "other")
    assert SttChain([a, b]).transcribe(AUDIO) == "hi" and b.calls == 0
    with pytest.raises(SttLimitReached):
        SttChain([P("g", SttLimitReached(30)), P("m", SttUnavailable("down"))]).transcribe(AUDIO)
    with pytest.raises(SttUnavailable):
        SttChain([P("g", SttUnavailable("a")), P("m", SttUnavailable("b"))]).transcribe(AUDIO)


def test_gemini_backup_needs_a_key_and_model_and_hides_empty_results():
    assert not GeminiSTT(api_key="", model="m").available and GeminiSTT(api_key="k", model="m").available

    class R:
        text = '  "Open Notepad"  '

    class C:
        class models:
            @staticmethod
            def generate_content(**kw):
                return R()

    assert GeminiSTT(client=C, model="m").transcribe(AUDIO) == "Open Notepad"
    assert GeminiSTT(client=C, model="m").transcribe(AUDIO[:100]) == ""


# ---- warm edge-tts ----------------------------------------------------------------------------------------------------------------------
class PlainFallback:
    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return "plain-audio"


def test_warm_edge_falls_back_to_plain_edge_when_the_connection_fails(monkeypatch):
    fb = PlainFallback()
    w = WarmEdgeTTS(fallback=fb, request_timeout=0.2, connect_timeout=0.2)

    async def boom(self, text):
        raise ConnectionError("dropped")

    async def noop(self):
        return None

    monkeypatch.setattr(WarmEdgeTTS, "_speak", boom)
    monkeypatch.setattr(WarmEdgeTTS, "_drop", noop)
    assert w.synthesize("Hello.") == "plain-audio" and fb.calls == ["Hello."] and w.stats["fallbacks"] == 1
    w.close()


def test_warm_edge_uses_plain_for_very_long_text_and_warm_never_raises():
    fb = PlainFallback()
    w = WarmEdgeTTS(fallback=fb)
    assert w.synthesize("x" * 4000) == "plain-audio"
    w._broken = True
    w.warm()                                                    # no-op when broken
    w.close()


def test_the_chain_and_speaker_expose_warm(monkeypatch):
    from voice.tts import TTSChain

    class Warmable:
        name = "w"
        warmed = 0

        def warm(self):
            self.warmed += 1

    p = Warmable()
    TTSChain([p]).warm()
    assert p.warmed == 1


def test_each_turn_warms_the_speech_connection():
    from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
    from ui.controller import VexController
    from ui.frontend import RecordingFrontend

    sp = FakeSpeaker()
    sp.warmed = 0
    sp.warm = lambda: setattr(sp, "warmed", sp.warmed + 1)
    c = VexController(FakeAgent(), RecordingFrontend(), sp, FakeCapture("hi"))
    c.start_listening("hotkey")
    assert c.wait_idle() and sp.warmed == 1


# ---- report ---------------------------------------------------------------------------------------------------------------------------------
def test_report_shows_audio_length_next_to_stt_and_backup_usage(capsys):
    rows = []
    for i, (aud, stt, fb, att) in enumerate([(2.0, 0.9, None, 1), (3.0, 4.6, True, 2), (1.0, 0.7, None, 1)]):
        rows.append({"ts": f"2026-10-04T18:0{i}:00", "source": "hotkey", "outcome": "spoken", "endpoint_ms": 700, "ends_clean": True, "audio_seconds": aud,
                     "stt_fallback": fb, "stt_attempts": att, "stages": {"endpoint_wait": 0.7, "stt": stt, "brain": 1.0, "tts_first_audio": 0.5, "total": 3.0}})
    d = latency_report.summary(rows)["stt_detail"]
    assert d["fallbacks"] == 1 and d["retries"] == 1 and d["median_audio_s"] == 2.0 and d["median_stt_s"] == 0.9
    latency_report.print_report(rows)
    out = capsys.readouterr().out
    assert "audio" in out and "backup STT used" in out and "STT retried" in out and "speech recognition: median" in out
