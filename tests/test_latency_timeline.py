"""The latency timeline, the mid-sentence-pause safeguard (recorder probe + capture), and the report tool."""
import json
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_voice_recorder import AmplitudeVad, FakeStream  # the recorder tests' fake microphone

from voice import timeline as tl
from voice.capture import VoiceCapture
from voice.recorder import Recorder, Recording, ResumeProbe
from voice.timeline import Timeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
import latency_report  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ---- the stopwatch --------------------------------------------------------------------------------------------------------------------
def test_stages_are_the_gaps_between_marks(tmp_path):
    c = Clock()
    t = Timeline(c, tmp_path / "latency.jsonl")
    t.begin("hotkey", 100.0)
    t.mark("record_start", 100.3)
    t.mark("speech_end", 102.0)
    t.mark("record_end", 102.72)
    t.mark("stt_start", 102.73)
    t.mark("stt_end", 103.55)
    t.llm("groq", 0.9, 8, 1100)
    t.tool("open_app", 0.2)
    t.mark("brain_end", 104.8)
    t.mark("first_audio", 105.7)
    t.set(endpoint_ms=700, words=5, ends_clean=True)
    rec = t.finish("spoken")
    s = rec["stages"]
    assert s == {"trigger_to_record": 0.3, "endpoint_wait": 0.72, "stt": 0.82, "brain": 1.25, "tts_first_audio": 0.9, "total": 3.7}
    assert rec["outcome"] == "spoken" and rec["llm_calls"] == 1 and rec["llm"][0]["provider"] == "groq" and rec["tools"][0]["name"] == "open_app"
    line = json.loads((tmp_path / "latency.jsonl").read_text(encoding="utf-8"))
    assert line["endpoint_ms"] == 700 and line["ts"].startswith("20")


def test_the_log_holds_timings_only_never_words(tmp_path):
    t = Timeline(Clock(), tmp_path / "l.jsonl")
    t.begin("hotkey", 1.0)
    t.set(words=4, ends_clean=True)
    t.finish("no_speech")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "words" in raw and "text" not in json.loads(raw)


def test_a_wake_word_turn_includes_the_wake_to_trigger_gap_and_an_old_wake_is_ignored(tmp_path):
    c = Clock()
    t = Timeline(c)
    t.note_wake(99.7)
    c.t = 100.0
    t.begin("wakeword")
    assert t._turn["marks"]["wake"] == 99.7
    t.finish()
    t.note_wake(50.0)                                           # an old detection belongs to some earlier turn
    t.begin("wakeword", 100.0)
    assert "wake" not in t._turn["marks"]
    t.note_wake(99.9)
    t.begin("hotkey", 100.0)                                    # only wake word turns use it
    assert "wake" not in t._turn["marks"]


def test_a_typed_turn_measures_from_the_trigger_and_a_turn_with_no_first_word_has_no_total(tmp_path):
    t = Timeline(Clock())
    t.begin("typed", 10.0)
    t.mark("brain_end", 11.5)
    t.mark("first_audio", 12.1)
    s = t.finish()["stages"]
    assert s["brain"] == 1.5 and s["total"] == 2.1
    t.begin("hotkey", 10.0)
    t.mark("speech_end", 11.0)
    t.mark("record_end", 11.7)
    assert "total" not in t.finish("error")["stages"]
    assert t.finish() is None                                   # nothing active: harmless


def test_marks_are_noops_without_a_turn_and_the_first_mark_wins():
    t = Timeline(Clock())
    t.mark("x", 1.0)
    t.llm("groq", 1, 1)
    t.set(a=1)
    t.begin("hotkey", 5.0)
    t.mark("stt_start", 6.0)
    t.mark("stt_start", 9.0)                                    # a second one is ignored...
    t.mark("stt_end", 7.0)
    t.mark("stt_end", 8.0, overwrite=True)                     # ...unless asked to overwrite
    assert t.finish()["stages"]["stt"] == 2.0


def test_failed_provider_attempts_are_recorded_too():
    t = Timeline(Clock())
    t.begin("hotkey", 1.0)
    t.llm("groq", 0.4, 8, ok=False)
    t.llm("gemini", 1.2, 8, 900)
    rec = t.finish()
    assert [(c["provider"], c["ok"]) for c in rec["llm"]] == [("groq", False), ("gemini", True)]


# ---- wired into the real parts ---------------------------------------------------------------------------------------------------------
def test_controller_executor_llm_and_speaker_feed_the_global_timeline(tmp_path, monkeypatch):
    from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
    from ui.controller import VexController
    from ui.frontend import RecordingFrontend

    fresh = Timeline(time.monotonic, tmp_path / "l.jsonl")
    for mod in ("ui.controller", "voice.capture", "voice.speaker", "brain.llm", "safety.executor"):
        monkeypatch.setattr(f"{mod}.timeline", fresh)
    c = VexController(FakeAgent(), RecordingFrontend(), FakeSpeaker(), FakeCapture("what time is it?"))
    assert c.start_listening("hotkey") == "started" and c.wait_idle()
    rec = json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["source"] == "hotkey" and rec["outcome"] == "spoken" and "brain" in rec["stages"]


def test_a_turn_that_hears_nothing_is_logged_as_no_speech(tmp_path, monkeypatch):
    from test_ui_core import FakeAgent, FakeCapture, FakeSpeaker
    from ui.controller import VexController
    from ui.frontend import RecordingFrontend

    fresh = Timeline(time.monotonic, tmp_path / "l.jsonl")
    monkeypatch.setattr("ui.controller.timeline", fresh)
    c = VexController(FakeAgent(), RecordingFrontend(), FakeSpeaker(), FakeCapture())
    c.start_listening("tray")
    assert c.wait_idle()
    assert json.loads((tmp_path / "l.jsonl").read_text(encoding="utf-8"))["outcome"] == "no_speech"


# ---- the safeguard: the recorder keeps listening a moment after the endpoint --------------------------------------------------------------
def make_rec(script, grace=500, silence_ms=300, **kw):
    opened = []

    def factory(device, samplerate, channels, dtype, blocksize, callback):
        s = FakeStream(script, samplerate, channels, blocksize, callback, period=0.004)
        opened.append(s)
        return s

    r = Recorder(silence_ms=silence_ms, stream_factory=factory, vad=AmplitudeVad(), grace_ms=grace, no_speech_seconds=3, **kw)
    r.streams = opened
    return r


def test_with_the_safeguard_off_the_microphone_closes_at_the_endpoint():
    r = make_rec([("speech", 20)], grace=0)
    rec = r.record()
    assert rec.reason == "speech" and rec.resume is None and r.streams[0]._stop.is_set()


def test_speech_that_carries_on_after_the_endpoint_is_captured_as_a_continuation():
    # 20 frames of speech, 11 of silence (>= 300 ms: the endpoint), then more speech 12 frames later, inside the 500 ms window
    r = make_rec([("speech", 20), ("silence", 11), ("silence", 4), ("speech", 14)])
    rec = r.record()
    assert rec.reason == "speech" and rec.resume is not None and rec.t_last_voiced >= rec.t_start > 0
    extra = rec.resume.wait(3)
    assert extra is not None and len(extra) >= 10 * 480 and rec.resume.resumed and rec.resume.gap_s is not None
    deadline = time.time() + 2
    while time.time() < deadline and not r.streams[0]._stop.is_set():
        time.sleep(0.01)
    assert r.streams[0]._stop.is_set()                                  # the probe closed the stream when it was done


def test_silence_after_the_endpoint_means_you_really_were_done():
    r = make_rec([("speech", 20), ("silence", 60)])
    rec = r.record()
    assert rec.resume.wait(3) is None and not rec.resume.resumed
    assert r.streams[0]._stop.is_set()


def test_a_tiny_blip_in_the_window_is_not_a_continuation():
    r = make_rec([("speech", 20), ("silence", 12), ("blip", 1), ("silence", 40)], resume_ms=120)
    rec = r.record()
    assert rec.resume.wait(3) is None


def test_cancelling_stops_the_probe_promptly():
    cancel = threading.Event()
    r = make_rec([("speech", 20)], grace=5000)                             # silence forever, a long window
    rec = r.record(cancel)
    t0 = time.time()
    cancel.set()
    assert rec.resume.wait(3) is None and time.time() - t0 < 1.5 and r.streams[0]._stop.is_set()


# ---- capture: the first recognition runs meanwhile, so the safeguard costs nothing unless you continue ---------------------------------------
class FakeProbe:
    def __init__(self, extra=None, delay=0.0, gap=0.4):
        self.extra, self.delay, self.gap_s, self.grace_ms, self.max_extra_s = extra, delay, gap, 500, 8.0
        self.stopped = False

    def wait(self, timeout=3.0):
        time.sleep(self.delay)
        return self.extra

    def stop(self):
        self.stopped = True


class FakeStt:
    available = True
    model = "fake"

    def __init__(self, texts, delay=0.0):
        self.texts, self.delay, self.calls = list(texts), delay, []

    def transcribe(self, audio, rate=16000):
        self.calls.append(len(audio))
        time.sleep(self.delay)
        if len(self.texts) == 2:                                    # (short audio, merged audio): answered by length, not by call order
            return self.texts[1] if len(audio) > 20000 else self.texts[0]
        return self.texts.pop(0)


def recording(probe, seconds=1.0):
    n = int(seconds * 16000)
    return Recording(np.ones(n, np.int16) * 500, "speech", seconds, seconds, 800.0, t_start=10.0, t_last_voiced=11.0, t_end=11.7, resume=probe)


def capture_with(rec, stt):
    return VoiceCapture(SimpleNamespace(record=lambda cancel: rec, silence_ms=700, grace_ms=500), stt)


def test_no_continuation_means_one_recognition_and_no_extra_wait():
    probe = FakeProbe(None)
    stt = FakeStt(["Open Notepad."], delay=0.2)
    t0 = time.time()
    assert capture_with(recording(probe), stt)() == "Open Notepad."
    assert len(stt.calls) == 1 and time.time() - t0 < 0.8 and probe.stopped


def test_a_continuation_is_transcribed_together_with_the_first_part():
    extra = np.ones(8000, np.int16) * 400
    probe = FakeProbe(extra, gap=0.6)
    stt = FakeStt(["Play some", "Play some jazz on YouTube."], delay=0.05)
    assert capture_with(recording(probe), stt)() == "Play some jazz on YouTube."
    assert sorted(stt.calls) == [16000, 16000 + 4000 + 8000]                    # the first part alone, then first part + 0.25 s of silence + the continuation


def test_a_failing_first_recognition_is_still_reported_when_nothing_followed():
    from voice.stt import SttUnavailable

    class Boom(FakeStt):
        def transcribe(self, audio, rate=16000):
            raise SttUnavailable("offline")

    cap = VoiceCapture(SimpleNamespace(record=lambda cancel: recording(FakeProbe(None)), silence_ms=700, grace_ms=500), Boom([]),
                       fallback=lambda: "typed instead")
    assert cap() == "typed instead"


def test_the_capture_marks_the_stages_and_flags_a_resumed_turn(monkeypatch):
    fresh = Timeline(time.monotonic)
    monkeypatch.setattr("voice.capture.timeline", fresh)
    fresh.begin("hotkey")
    stt = FakeStt(["Play some", "Play some jazz."], delay=0.01)
    VoiceCapture(SimpleNamespace(record=lambda cancel: recording(FakeProbe(np.ones(4000, np.int16), gap=0.5)), silence_ms=700, grace_ms=500), stt)()
    turn = fresh._turn
    assert turn["data"]["resumed"] is True and turn["data"]["resume_gap"] == 0.5 and turn["data"]["endpoint_ms"] == 700
    assert turn["data"]["words"] == 3 and turn["data"]["ends_clean"] is True
    assert turn["marks"]["speech_end"] == 11.0 and turn["marks"]["record_end"] == 11.7 and "stt_end" in turn["marks"]


# ---- the report -------------------------------------------------------------------------------------------------------------------------
def row(ts, total, endpoint_ms=700, wait=0.7, stt=0.8, brain=1.0, tts=0.8, resumed=False, clean=True, outcome="spoken", gap=None, calls=1):
    r = {"ts": ts.isoformat(timespec="seconds"), "source": "hotkey", "outcome": outcome, "endpoint_ms": endpoint_ms, "ends_clean": clean,
         "stages": {"endpoint_wait": wait, "stt": stt, "brain": brain, "tts_first_audio": tts, "total": total}, "llm_calls": calls,
         "llm": [{"provider": "groq", "seconds": 1.0, "tools": 8, "ok": True}] * calls}
    if resumed:
        r.update(resumed=True, resume_gap=gap)
    return r


def test_the_report_summarises_stages_endpoints_and_cutoffs(tmp_path):
    now = datetime(2026, 10, 4, 18, 0, 0)
    rows = [row(now + timedelta(seconds=60 * i), 3.0 + i * 0.1) for i in range(8)]
    rows[2] = row(now + timedelta(seconds=120), 4.6, resumed=True, gap=0.55)
    rows[4] = row(now + timedelta(seconds=240), 3.2, clean=False)                                    # no full stop...
    rows[5] = row(now + timedelta(seconds=250), 3.0)                                                 # ...and another turn 10 s later
    rows.append(row(now + timedelta(seconds=900), 2.5, endpoint_ms=1000, wait=1.0))
    s = latency_report.summary(rows)
    assert s["turns"] == 9 and s["spoken"] == 9
    assert s["stages"]["stt"]["median"] == 0.8 and s["stages"]["total"]["n"] == 9
    c = s["cutoffs"]
    assert c["voice_turns"] == 9 and c["resumed"] == 1 and c["median_resume_gap_s"] == 0.55 and c["possible_residual_cutoffs"] == 1
    assert set(s["by_endpoint_ms"]) == {"700", "1000"} and s["by_endpoint_ms"]["700"]["turns"] == 8
    assert s["by_endpoint_ms"]["700"]["estimated_median_total_at_1000ms"] == pytest.approx(s["by_endpoint_ms"]["700"]["median_total"] + 0.3)
    assert s["slowest"][0][0] in ("brain", "stt", "tts_first_audio", "endpoint_wait")


def test_report_command_prints_a_table_and_handles_an_empty_log(tmp_path, capsys):
    p = tmp_path / "latency.jsonl"
    assert latency_report.main(["--file", str(p)]) == 0 and "No turns logged yet" in capsys.readouterr().out
    now = datetime(2026, 10, 4, 18, 0, 0)
    p.write_text("\n".join(json.dumps(row(now + timedelta(minutes=i), 3.0, calls=2 if i == 1 else 1)) for i in range(3)) + "\n", encoding="utf-8")
    assert latency_report.main(["--file", str(p), "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out and "2 model calls" in out and "Cut-offs:" in out and "silence setting 700 ms" in out
    assert latency_report.main(["--file", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["spoken"] == 3
    assert latency_report.main(["--file", str(p), "--since", "2026-10-04 18:01:30"]) == 0
    assert "1 spoken turns of 1" in capsys.readouterr().out


def test_percentiles():
    assert latency_report.percentile([1, 2, 3, 4], 0.5) == 2.5 and latency_report.percentile([5], 0.9) == 5 and latency_report.percentile([], 0.5) != latency_report.percentile([], 0.5)


def test_the_silence_endpoint_and_safeguard_are_settings_starting_at_700_ms():
    import config

    v = config.settings["voice"]
    assert v["silence_ms"] == 700 and v["resume_grace_ms"] == 500 and v["resume_min_ms"] == 120
    from voice.factory import build_voice

    stack = build_voice({**v, "enabled": True}, root=Path("."))
    assert stack.recorder.silence_ms == 700 and stack.recorder.grace_ms == 500 and stack.recorder.resume_ms == 120
