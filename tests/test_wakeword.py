import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import config
from voice.recorder import FRAME, Recorder
from voice.wakeword import CHUNK, WakeWordListener, label_for, make_chime, strip_wake_phrase


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = True

    def start(self):
        pass

    def stop(self):
        self.active = False

    def close(self):
        pass


class FakeMic:
    """Stands in for sounddevice.InputStream: tests push 30 ms blocks through the callback."""

    def __init__(self, fail_first=0):
        self.streams: list[FakeStream] = []
        self.fail_first = fail_first
        self.opens = 0

    def __call__(self, **kw):
        self.opens += 1
        if self.opens <= self.fail_first:
            raise OSError("no microphone")
        s = FakeStream(kw["callback"])
        self.streams.append(s)
        return s

    def push(self, n_frames=1, value=100):
        for _ in range(n_frames):
            self.streams[-1].callback(np.full((FRAME, 1), value, np.int16), FRAME, None, None)


class FakeModel:
    """predict() returns the next scripted score (default 0)."""

    def __init__(self):
        self.scores: list[float] = []
        self.resets = 0
        self.chunks = 0

    def predict(self, chunk):
        assert len(chunk) == CHUNK
        self.chunks += 1
        return {"hey_jarvis": self.scores.pop(0) if self.scores else 0.0}

    def reset(self):
        self.resets += 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig(tmp_path):
    made = []

    def build(**kw):
        mic, model, clock = kw.pop("mic", FakeMic()), kw.pop("model", FakeModel()), kw.pop("clock", Clock())
        wakes = kw.pop("wakes", [])
        l = WakeWordListener(kw.pop("on_wake", lambda: wakes.append(1)), root=tmp_path, stream_factory=mic,
                             model_factory=lambda m, v: model, clock=clock, retry_seconds=kw.pop("retry_seconds", 0.05), **kw)
        l.start()
        made.append(l)
        assert wait_for(lambda: mic.streams), "listener never opened the microphone"
        return l, mic, model, clock, wakes

    yield build
    for l in made:
        l.stop()


def feed_chunks(mic, n):
    """n scored chunks = n * 1280/480 frames; push a bit more than needed."""
    mic.push(int(n * CHUNK / FRAME) + 3)


# ---- model choice ---------------------------------------------------------------------------------------------------------------------
def test_falls_back_to_hey_jarvis_when_the_custom_model_is_missing(tmp_path):
    l = WakeWordListener(lambda: None, model_path="models/hey_vex.onnx", root=tmp_path)
    assert l.using_fallback and l.active_model == "hey_jarvis" and l.label == "Hey Jarvis (fallback)"


def test_uses_the_custom_model_when_it_exists(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "hey_vex.onnx").write_bytes(b"x")
    l = WakeWordListener(lambda: None, model_path="models/hey_vex.onnx", root=tmp_path)
    assert not l.using_fallback and l.label == "Hey Vex" and l.active_model.endswith("hey_vex.onnx")
    assert label_for("hey_jarvis_v0.1.onnx") == "Hey Jarvis (fallback)"


# ---- firing ---------------------------------------------------------------------------------------------------------------------------
def test_a_score_above_threshold_fires_once(rig):
    wakes = []
    l, mic, model, clock, _ = rig(wakes=wakes, threshold=0.5)
    clock.t += 5                                                 # past the settle period
    model.scores = [0.1, 0.2, 0.9, 0.95, 0.9]
    feed_chunks(mic, 6)
    assert wait_for(lambda: wakes) and wait_for(lambda: model.chunks >= 3)
    time.sleep(0.2)
    assert len(wakes) == 1 and l.paused                          # it paused itself, so the later high scores didn't count


def test_two_quick_triggers_within_the_cooldown_fire_once(rig):
    wakes = []
    l, mic, model, clock, _ = rig(wakes=wakes, cooldown=2.0)
    clock.t += 5
    model.scores = [0.9]
    feed_chunks(mic, 2)
    assert wait_for(lambda: len(wakes) == 1)
    l.resume()
    clock.t += 1.0                                               # settle over, but still inside the 2 s cooldown
    model.scores = [0.9]
    feed_chunks(mic, 3)
    time.sleep(0.3)
    assert len(wakes) == 1
    clock.t += 3.0
    model.scores = [0.9]
    feed_chunks(mic, 3)
    assert wait_for(lambda: len(wakes) == 2)


def test_while_paused_high_scores_never_fire(rig):
    wakes = []
    l, mic, model, clock, _ = rig(wakes=wakes)
    clock.t += 5
    l.pause()
    model.scores = [0.99] * 10
    feed_chunks(mic, 10)
    time.sleep(0.3)
    assert wakes == [] and model.chunks == 0


def test_resume_resets_the_model_and_ignores_the_first_moments(rig):
    wakes = []
    l, mic, model, clock, _ = rig(wakes=wakes)
    clock.t += 5
    l.pause()
    l.resume()
    before = model.resets
    model.scores = [0.99] * 3                                    # Vex's own voice tail right after resuming
    feed_chunks(mic, 3)
    assert wait_for(lambda: model.resets > before)
    time.sleep(0.2)
    assert wakes == []                                           # inside the settle window
    clock.t += 5
    model.scores = [0.99]
    feed_chunks(mic, 2)
    assert wait_for(lambda: wakes)


def test_the_listener_resumes_even_when_the_handler_raises(rig, caplog):
    def boom():
        raise RuntimeError("handler failed")

    with caplog.at_level(logging.INFO, logger="vex.wakeword"):
        l, mic, model, clock, _ = rig(on_wake=boom)
        clock.t += 5
        model.scores = [0.9]
        feed_chunks(mic, 2)
        assert wait_for(lambda: any("wake handler failed" in r.message for r in caplog.records))
    assert wait_for(lambda: not l.paused)


@pytest.mark.parametrize("result", ["muted", "busy"])
def test_no_turn_started_means_the_listener_resumes_itself(rig, result):
    l, mic, model, clock, _ = rig(on_wake=lambda: result)
    clock.t += 5
    model.scores = [0.9]
    feed_chunks(mic, 2)
    assert wait_for(lambda: l.wakes == 1) and wait_for(lambda: not l.paused)


def test_a_started_turn_stays_paused_until_the_turn_resumes_it(rig):
    l, mic, model, clock, _ = rig(on_wake=lambda: "started")
    clock.t += 5
    model.scores = [0.9]
    feed_chunks(mic, 2)
    assert wait_for(lambda: l.wakes == 1)
    time.sleep(0.2)
    assert l.paused
    l.resume()
    assert not l.paused


# ---- stage logging --------------------------------------------------------------------------------------------------------------------
def test_wake_pause_and_resume_are_logged_with_the_score(rig, caplog):
    with caplog.at_level(logging.INFO, logger="vex.wakeword"):
        l, mic, model, clock, _ = rig(on_wake=lambda: "started")
        clock.t += 5
        model.scores = [0.87]
        feed_chunks(mic, 2)
        assert wait_for(lambda: l.wakes == 1)
        l.resume()
    text = " | ".join(r.message for r in caplog.records)
    assert "wake detected: Hey Jarvis (fallback) score 0.87" in text and "(threshold 0.50); paused" in text and "wake word resumed" in text


# ---- audio after the wake word ---------------------------------------------------------------------------------------------------------
def test_audio_heard_after_the_wake_word_is_handed_over_once(rig):
    holder = {}

    def on_wake():
        holder["audio"] = None
        return "started"

    l, mic, model, clock, _ = rig(on_wake=on_wake)
    clock.t += 5
    model.scores = [0.9]
    feed_chunks(mic, 1)
    assert wait_for(lambda: l.wakes == 1)
    mic.push(10, value=777)                                      # the user keeps talking: "...open notepad"
    assert wait_for(lambda: l._after and len(l._after) >= 10)
    audio = l.take_audio()
    assert audio is not None and len(audio) >= 10 * FRAME and (audio[-FRAME:] == 777).all()
    assert l.take_audio() is None


def test_take_audio_is_none_if_the_pause_was_not_a_wake(rig):
    l, mic, model, clock, _ = rig()
    l.pause()
    mic.push(10)
    time.sleep(0.2)
    assert l.take_audio() is None


def test_the_recorder_treats_seed_audio_as_the_start_of_the_utterance():
    stop = threading.Event()

    class Vad:
        def __init__(self):
            self.n = 0

        def is_speech(self, frame):
            self.n += 1
            return self.n <= 12                                  # only the seeded frames are speech

    def factory(**kw):
        def feed():
            while not stop.is_set():
                kw["callback"](np.zeros((FRAME, 1), np.int16), FRAME, None, None)
                time.sleep(0.002)

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        return SimpleNamespace(start=lambda: None, stop=stop.set, close=lambda: None, active=True)

    r = Recorder(vad=Vad(), stream_factory=factory, silence_ms=300)
    r.seed = np.full(12 * FRAME, 500, np.int16)
    rec = r.record()
    stop.set()
    assert rec.reason == "speech" and rec.speech_seconds >= 0.3 and (rec.audio[:FRAME] == 500).all()
    assert r.seed is None                                         # one-shot


# ---- microphone trouble -----------------------------------------------------------------------------------------------------------------
def test_a_missing_microphone_notifies_once_and_retries(rig, caplog):
    notes = []
    mic = FakeMic(fail_first=2)
    with caplog.at_level(logging.INFO, logger="vex.wakeword"):
        l = WakeWordListener(lambda: None, stream_factory=mic, model_factory=lambda m, v: FakeModel(), retry_seconds=0.05,
                             notify=lambda t, m: notes.append(m), root=config.ROOT / "nonexistent")
        l.start()
        try:
            assert wait_for(lambda: mic.streams and l.running)
        finally:
            l.stop()
    assert notes == ["Wake word unavailable. The hotkey still works."]
    text = " | ".join(r.message for r in caplog.records)
    assert "wake word unavailable" in text and "wake word recovered" in text


def test_a_stream_that_dies_is_reopened(rig):
    l, mic, model, clock, _ = rig()
    assert wait_for(lambda: l.running)
    mic.streams[0].active = False                                # headphones unplugged, default device changed
    assert wait_for(lambda: mic.opens >= 2 and len(mic.streams) >= 2, timeout=6)


def test_a_model_that_fails_to_load_does_not_crash(tmp_path):
    notes = []

    def bad(model, vad):
        raise FileNotFoundError("no such model")

    l = WakeWordListener(lambda: None, stream_factory=FakeMic(), model_factory=bad, retry_seconds=0.05, notify=lambda t, m: notes.append(m),
                         root=tmp_path)
    l.start()
    try:
        assert wait_for(lambda: notes)
    finally:
        l.stop()
    assert not l.running


# ---- config ---------------------------------------------------------------------------------------------------------------------------
def test_invalid_config_values_fall_back_to_defaults(monkeypatch, caplog):
    monkeypatch.setitem(config.settings, "wakeword", {"enabled": "yes", "threshold": 7, "cooldown": "soon", "vad_threshold": -1,
                                                       "model_path": 5, "chime": False, "input_device": "Realtek"})
    with caplog.at_level(logging.WARNING):
        c = config.wakeword_config()
    d = config.DEFAULTS["wakeword"]
    assert c["enabled"] == d["enabled"] and c["threshold"] == d["threshold"] and c["cooldown"] == d["cooldown"]
    assert c["vad_threshold"] == d["vad_threshold"] and c["model_path"] == d["model_path"]
    assert c["chime"] is False and c["input_device"] == "Realtek"                  # valid values are kept
    assert sum("is not valid" in r.message for r in caplog.records) == 5


def test_valid_numbers_may_be_ints(monkeypatch):
    monkeypatch.setitem(config.settings, "wakeword", {"threshold": 1 / 2, "cooldown": 3})
    assert config.wakeword_config()["cooldown"] == 3.0


# ---- helpers --------------------------------------------------------------------------------------------------------------------------
def test_strip_wake_phrase():
    assert strip_wake_phrase("Hey Vex, open notepad") == "open notepad"
    assert strip_wake_phrase("hey jarvis what's the time?") == "what's the time?"
    assert strip_wake_phrase("Vex. play lofi") == "play lofi"
    assert strip_wake_phrase("open notepad") == "open notepad"
    assert strip_wake_phrase("vexillology is fun") == "vexillology is fun"
    assert strip_wake_phrase("tell hey vex jokes") == "tell hey vex jokes"        # only at the start
    assert strip_wake_phrase("Hey Vex") == ""


def test_the_chime_is_short_soft_and_click_free():
    c = make_chime()
    assert 0.12 < len(c) / 24000 < 0.2 and abs(c).max() < 0.3 and abs(c[0]) < 0.01 and abs(c[-1]) < 0.01


# ---- wiring in the app ----------------------------------------------------------------------------------------------------------------
def make_stack():
    return SimpleNamespace(recorder=SimpleNamespace(device=None, seed=None))


def test_the_wake_handler_chimes_seeds_the_recorder_and_uses_the_single_trigger_path(monkeypatch):
    import ui.app as app
    import voice.wakeword as ww

    chimes = []
    monkeypatch.setattr(ww, "play_chime", lambda *a, **k: chimes.append(1))
    monkeypatch.setitem(config.settings, "wakeword", {**config.DEFAULTS["wakeword"], "chime": True})
    triggers = []
    ctl = SimpleNamespace(mic_muted=False, on_interaction_start=[], on_interaction_end=[],
                          start_listening=lambda source="hotkey", typed=False: triggers.append(source) or "started")
    stack = make_stack()
    l = app.build_wakeword(ctl, stack, lambda t, m: None)
    audio = np.full(5 * FRAME, 9, np.int16)
    l._after.extend(np.split(audio, 5))
    l._woke = True
    assert l.on_wake() == "started"
    assert triggers == ["wakeword"] and chimes == [1]
    assert stack.recorder.seed == l.take_audio                                           # fetched when recording starts, not now
    got = l.take_audio()
    assert got is not None and len(got) == 5 * FRAME and l.take_audio() is None         # the words spoken during the chime are kept
    assert ctl.on_interaction_start == [l.pause] and ctl.on_interaction_end == [l.resume]


def test_a_muted_microphone_skips_the_chime(monkeypatch):
    import ui.app as app
    import voice.wakeword as ww

    chimes = []
    monkeypatch.setattr(ww, "play_chime", lambda *a, **k: chimes.append(1))
    ctl = SimpleNamespace(mic_muted=True, on_interaction_start=[], on_interaction_end=[], start_listening=lambda source="x", typed=False: "muted")
    l = app.build_wakeword(ctl, make_stack(), lambda t, m: None)
    assert l.on_wake() == "muted" and chimes == []


def test_no_voice_means_no_wake_word():
    import ui.app as app

    assert app.build_wakeword(SimpleNamespace(), None, lambda t, m: None) is None


# ---- calibration tool -----------------------------------------------------------------------------------------------------------------
def tune():
    import importlib.util

    spec = importlib.util.spec_from_file_location("wakeword_tune", config.ROOT / "tools_dev" / "wakeword_tune.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_threshold_suggestion_sits_between_real_and_false_scores():
    t = tune()
    v, why = t.suggest_threshold([0.8, 0.9, 0.7], 0.3)
    assert v == 0.5 and "midpoint" in why
    v, why = t.suggest_threshold([0.4, 0.9], 0.6)                    # overlap: favour no false wakes, warn about misses
    assert v == 0.65 and "overlap" in why
    assert t.suggest_threshold([], None)[0] == 0.5
    assert t.suggest_threshold([0.9], None)[0] <= 0.7 and t.suggest_threshold([], 0.5)[0] >= 0.5


def test_score_bar_marks_the_threshold():
    t = tune()
    assert t.bar(0.0, 0.5, 10) == ".....|...." and t.bar(1.0, 0.5, 10) == "#####!####"


def test_a_seed_function_is_called_when_recording_starts_and_a_failed_trigger_clears_it():
    import ui.app as app

    ctl = SimpleNamespace(mic_muted=False, on_interaction_start=[], on_interaction_end=[],
                          start_listening=lambda source="x", typed=False: "busy")
    stack = make_stack()
    l = app.build_wakeword(ctl, stack, lambda t, m: None)
    assert l.on_wake() == "busy" and stack.recorder.seed is None

    got = []
    r = Recorder(vad=SimpleNamespace(is_speech=lambda f: False), stream_factory=lambda **kw: SimpleNamespace(
        start=lambda: None, stop=lambda: None, close=lambda: None, active=True))
    r.seed = lambda: got.append(1) or np.zeros(3 * FRAME, np.int16)
    r.record(max_seconds=0.2, no_speech_seconds=0.1)
    assert got == [1] and r.seed is None


def test_the_agent_knows_the_current_time():
    from datetime import datetime, timezone

    from brain.agent import now_text

    assert now_text(datetime(2026, 10, 4, 0, 53, tzinfo=timezone.utc)) == "Sunday 4 October 2026, 12:53 AM (UTC)"
    assert now_text(datetime(2026, 10, 14, 15, 5, tzinfo=timezone.utc)).startswith("Wednesday 14 October 2026, 3:05 PM")


def test_take_audio_includes_frames_still_queued(rig):
    l, mic, model, clock, _ = rig(on_wake=lambda: "started")
    clock.t += 5
    model.scores = [0.9]
    feed_chunks(mic, 1)
    assert wait_for(lambda: l.wakes == 1)
    mic.push(6, value=321)
    audio = l.take_audio()                                       # asked for immediately, before the listener thread moved them
    assert audio is not None and len(audio) >= 4 * FRAME


import pytest  # noqa: E402


@pytest.mark.parametrize("raw,clean", [
    ("Have x.", ""), ("Havix,", ""), ("Hey Vex, open notepad", "open notepad"), ("hey x what's the time", "what's the time"),
    ("Ex, play lofi", "play lofi"), ("backs open chrome", "open chrome"), ("Hey becks", ""), ("wex", ""), ("Hey vecs.", ""),
    ("Havix open notepad", "open notepad"), ("heyvex play", "play"), ("VEX!", "")])
def test_whisper_mishearings_of_the_wake_phrase_are_stripped(raw, clean):
    assert strip_wake_phrase(raw) == clean


@pytest.mark.parametrize("text", ["vexillology is fun", "open notepad", "next song", "text mom", "Heavy metal", "Having fun",
                                  "Hexagon is a shape", "Hello world", "vet appointment", "Have a nice day", "tell hey vex jokes"])
def test_ordinary_requests_are_not_mistaken_for_the_wake_phrase(text):
    assert strip_wake_phrase(text) == text
