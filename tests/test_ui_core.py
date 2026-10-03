"""UI logic that doesn't need Qt: frontend protocol, audio levels, controller, hotkey, config persistence."""
import json
import threading
import time
import tomllib

import numpy as np
import pytest

import config
from ui import frontend as fe
from ui.controller import IDLE, LISTENING, SPEAKING, THINKING, VexController
from ui.frontend import JsonLinesFrontend, RecordingFrontend, clamp01, decode, dispatch
from ui.hotkey import GlobalHotkey
from ui.levels import LevelPump, SoundDevicePlayer, rms_levels
from ui.speaker import SimulatedSpeaker


# ---- frontend protocol -------------------------------------------------------------------------------------------------
def test_clamp01():
    assert [clamp01(x) for x in (-1, 0.5, 7, float("nan"), "x", None)] == [0.0, 0.5, 1.0, 0.0, 0.0, 0.0]


def test_json_lines_roundtrip_drives_another_frontend():
    sent: list[str] = []
    out = JsonLinesFrontend(sent.append)
    out.show()
    out.set_state("listening")
    out.show_caption("héllo “there”")
    out.set_state("speaking")
    for lv in (0.1, 0.1, 0.5, 2.0):
        out.set_level(lv)
    out.hide()
    assert all(s.endswith("\n") for s in sent)
    assert [json.loads(s)["op"] for s in sent] == ["show", "state", "caption", "state", "level", "level", "level", "hide"]
    remote = RecordingFrontend()
    for line in sent:
        dispatch(remote, decode(line))
    assert remote.calls == [("show", None), ("state", "listening"), ("caption", "héllo “there”"), ("state", "speaking"),
                            ("level", 0.1), ("level", 0.5), ("level", 1.0), ("hide", None)]


def test_json_lines_drops_repeated_levels_but_not_after_a_state_change():
    sent: list[str] = []
    out = JsonLinesFrontend(sent.append)
    out.set_level(0.3333333)
    out.set_level(0.3334)
    out.set_state("idle")
    out.set_level(0.333)
    assert [json.loads(s).get("level") for s in sent] == [0.333, None, 0.333]


def test_json_lines_swallows_write_failures_and_rejects_bad_states():
    def broken(_):
        raise BrokenPipeError("frontend is gone")

    out = JsonLinesFrontend(broken)
    out.set_state("idle")                      # must not raise
    out.set_level(0.5)
    with pytest.raises(ValueError):
        out.set_state("dancing")


@pytest.mark.parametrize("line", ["[]", '{"nope": 1}', '{"op": "explode"}', '{"op": "state", "state": "x"}',
                                  '{"op": "level", "level": "loud"}', '{"op": "caption", "text": 5}', "not json"])
def test_decode_rejects_malformed_messages(line):
    with pytest.raises(ValueError):
        decode(line)


def test_states_are_the_documented_four():
    assert fe.STATES == ("idle", "listening", "thinking", "speaking") and fe.LEVEL_HZ == 30


# ---- levels ------------------------------------------------------------------------------------------------------------
def test_rms_levels_shape_and_values():
    sr = 16000
    t = np.arange(sr) / sr
    tone = (0.25 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    levels = rms_levels(tone, sr)
    assert len(levels) == 31 and all(0 <= v <= 1 for v in levels)          # 1 s at 33 ms hops
    assert levels[5] == pytest.approx(0.25 / np.sqrt(2) * 4, rel=0.05)
    assert rms_levels(np.zeros(sr, dtype=np.float32), sr) == [0.0] * 31
    assert rms_levels(np.array([]), sr) == []
    assert max(rms_levels((tone * 32767).astype(np.int16), sr)) == pytest.approx(max(levels), rel=0.02)
    stereo = np.stack([tone, tone], axis=1)
    assert rms_levels(stereo, sr)[5] == pytest.approx(levels[5], rel=0.01)
    assert max(rms_levels(np.ones(sr, dtype=np.float32), sr)) == 1.0       # clipped, never above 1


def test_level_pump_pushes_about_30_per_second_and_ends_at_zero():
    got: list[tuple[float, float]] = []
    t0 = time.monotonic()
    pump = LevelPump(lambda v: got.append((time.monotonic() - t0, v)), [0.5] * 15)   # 15 hops = 0.5 s
    pump.start()
    pump.join(3)
    assert 13 <= len(got) - 1 <= 16, len(got)          # 15 levels, no drift
    assert got[-1][1] == 0.0 and all(v == 0.5 for _t, v in got[:-1])
    gaps = [b[0] - a[0] for a, b in zip(got[:-2], got[1:-1])]
    assert 0.030 < sum(gaps) / len(gaps) < 0.037, sum(gaps) / len(gaps)          # ~30 Hz


def test_level_pump_stops_promptly():
    got = []
    pump = LevelPump(got.append, [0.7] * 3000)
    pump.start()
    time.sleep(0.15)
    t = time.monotonic()
    pump.stop()
    assert time.monotonic() - t < 0.5 and got[-1] == 0.0


class FakeSD:
    def __init__(self):
        self.played = []
        self.stopped = threading.Event()

    def play(self, x, sr):
        self.played.append((len(x), sr))
        self.stopped.clear()

    def wait(self):
        self.stopped.wait(2)

    def stop(self):
        self.stopped.set()


def test_sounddevice_player_pumps_levels_and_can_be_stopped():
    sd = FakeSD()
    player = SoundDevicePlayer(sd)
    levels = []
    samples = (0.3 * np.sin(np.arange(16000) / 5)).astype(np.float32)
    t = threading.Thread(target=player.play, args=(samples, 16000, levels.append))
    t.start()
    time.sleep(0.2)
    assert sd.played == [(16000, 16000)] and any(v > 0 for v in levels)
    player.stop()                                          # barge-in
    t.join(2)
    assert not t.is_alive() and levels[-1] == 0.0


def test_simulated_speaker_emits_levels_then_zero_and_stops():
    sp = SimulatedSpeaker()
    levels = []
    t = threading.Thread(target=sp.speak, args=("one two three four five six seven eight", levels.append))
    t.start()
    time.sleep(0.4)
    sp.stop()
    t.join(2)
    assert not t.is_alive() and levels[-1] == 0.0 and 8 <= len(levels) <= 20
    assert all(0 <= v <= 1 for v in levels)
    assert sp.duration("word " * 100) == sp.max_seconds and sp.duration("hi") == 0.8


# ---- controller --------------------------------------------------------------------------------------------------------
class FakeAgent:
    def __init__(self, fail=False):
        self.heard: list[str] = []
        self.fail = fail

    def run(self, text):
        self.heard.append(text)
        if self.fail:
            raise RuntimeError("llm exploded")
        return f"reply to {text}"


class FakeCapture:
    def __init__(self, *texts, block=None, fail=False):
        self.texts = list(texts)
        self.block = block
        self.fail = fail
        self.calls = 0
        self.cancelled = threading.Event()

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("microphone unplugged")
        if self.block:
            self.block.wait(3)
        return self.texts.pop(0) if self.texts else None

    def cancel(self):
        self.cancelled.set()
        if self.block:
            self.block.set()


class FakeSpeaker:
    def __init__(self, hold=None):
        self.spoken: list[str] = []
        self.stops = 0
        self.hold = hold                      # Event: speak() blocks until it is set or stop() is called
        self.started = threading.Event()
        self._stop = threading.Event()

    def speak(self, text, on_level):
        self.spoken.append(text)
        self._stop.clear()
        on_level(0.6)
        self.started.set()
        if self.hold is not None:
            while not (self._stop.is_set() or self.hold.is_set()):
                time.sleep(0.005)
        on_level(0.0)

    def stop(self):
        self.stops += 1
        self._stop.set()


def make(agent=None, capture=None, speaker=None):
    fe_ = RecordingFrontend()
    c = VexController(agent or FakeAgent(), fe_, speaker or FakeSpeaker(), capture or FakeCapture("open notepad"))
    c.hooks = {"start": 0, "end": 0}
    c.on_interaction_start.append(lambda: c.hooks.__setitem__("start", c.hooks["start"] + 1))
    c.on_interaction_end.append(lambda: c.hooks.__setitem__("end", c.hooks["end"] + 1))
    return c, fe_


def test_a_full_turn_walks_through_every_state():
    c, f = make()
    seen = []
    c.listeners.append(seen.append)
    assert c.start_listening("hotkey") == "started"
    assert c.wait_idle()
    assert f.states == [LISTENING, THINKING, SPEAKING, IDLE] == seen
    assert [v for op, v in f.calls if op == "caption"] == ["“open notepad”", "reply to open notepad"]
    levels = [v for op, v in f.calls if op == "level"]
    assert 0.6 in levels and levels[-1] == 0.0 and f.calls[-1] == ("state", "idle")
    assert c.agent.heard == ["open notepad"] and c.speaker.spoken == ["reply to open notepad"]
    assert c.hooks == {"start": 1, "end": 1} and c.state == IDLE


def test_every_trigger_goes_through_the_same_function():
    for source in ("hotkey", "tray", "click", "wakeword"):
        c, f = make()
        assert c.start_listening(source) == "started" and c.wait_idle()
        assert f.states == [LISTENING, THINKING, SPEAKING, IDLE]


def test_nothing_heard_goes_back_to_idle_without_calling_the_agent():
    c, f = make(capture=FakeCapture(None))
    c.start_listening()
    assert c.wait_idle() and f.states == [LISTENING, IDLE] and c.agent.heard == []
    c2, f2 = make(capture=FakeCapture("   "))
    c2.start_listening()
    c2.wait_idle()
    assert c2.agent.heard == []


def test_triggers_while_listening_or_thinking_are_ignored():
    gate = threading.Event()
    c, f = make(capture=FakeCapture("hi", block=gate))
    assert c.start_listening() == "started"
    time.sleep(0.05)
    assert c.start_listening("hotkey") == "busy" and c.start_listening("click") == "busy"
    gate.set()
    assert c.wait_idle() and c.capture.calls == 1 and c.agent.heard == ["hi"]


def test_hotkey_while_speaking_stops_speech_and_starts_listening():
    hold = threading.Event()
    sp = FakeSpeaker(hold=hold)
    c, f = make(capture=FakeCapture("first", "second"), speaker=sp)
    c.start_listening()
    assert sp.started.wait(2)
    assert c.state == SPEAKING
    assert c.start_listening("hotkey") == "barge_in"
    time.sleep(0.15)
    assert sp.stops == 1
    hold.set()                                               # let the second reply finish
    assert c.wait_idle(3)
    assert f.states == [LISTENING, THINKING, SPEAKING, LISTENING, THINKING, SPEAKING, IDLE]
    assert c.agent.heard == ["first", "second"] and c.hooks == {"start": 1, "end": 1}      # one bracketed interaction


def test_muted_microphone_blocks_listening():
    c, f = make()
    c.set_mic_muted(True)
    assert c.start_listening() == "muted" and c.agent.heard == [] and f.states == []
    assert ("caption", "The microphone is muted.") in f.calls
    c.set_mic_muted(False)
    assert c.start_listening() == "started" and c.wait_idle()


def test_muting_while_listening_cancels_the_capture():
    gate = threading.Event()
    cap = FakeCapture("never", block=gate)
    c, f = make(capture=cap)
    c.start_listening()
    time.sleep(0.05)
    c.set_mic_muted(True)
    assert cap.cancelled.is_set() and c.wait_idle()


def test_agent_error_ends_in_idle_with_a_message_and_hooks_still_run():
    c, f = make(agent=FakeAgent(fail=True))
    c.start_listening()
    assert c.wait_idle() and c.state == IDLE and f.states[-1] == IDLE
    assert ("caption", "Something went wrong. Try again.") in f.calls and c.speaker.spoken == []
    assert c.hooks == {"start": 1, "end": 1}
    assert c.start_listening() == "started"                  # and it can be used again
    c.wait_idle()


def test_capture_error_ends_in_idle_and_hooks_still_run():
    c, f = make(capture=FakeCapture(fail=True))
    c.start_listening()
    assert c.wait_idle() and f.states == [LISTENING, IDLE] and c.hooks == {"start": 1, "end": 1}


def test_a_broken_hook_does_not_wedge_the_interaction():
    c, f = make()
    c.on_interaction_start.insert(0, lambda: 1 / 0)
    c.on_interaction_end.insert(0, lambda: 1 / 0)
    c.start_listening()
    assert c.wait_idle() and f.states[-1] == IDLE and c.hooks == {"start": 1, "end": 1}


def test_many_simultaneous_triggers_start_exactly_one_turn():
    gate = threading.Event()
    c, f = make(capture=FakeCapture("hi", block=gate))
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.start_listening("hotkey"))) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gate.set()
    c.wait_idle()
    assert sorted(results).count("started") == 1 and c.capture.calls == 1


def test_shutdown_stops_speech_and_capture():
    hold = threading.Event()
    sp = FakeSpeaker(hold=hold)
    c, f = make(speaker=sp)
    c.start_listening()
    assert sp.started.wait(2)
    c.shutdown()
    assert c.wait_idle(3) and sp.stops >= 1 and c.start_listening() == "busy"


# ---- hotkey ------------------------------------------------------------------------------------------------------------
class FakeKeyboard:
    def __init__(self, fail=None):
        self.fail = fail
        self.registered = {}
        self.removed = []

    def add_hotkey(self, combo, cb, suppress=False, trigger_on_release=False):
        if self.fail:
            raise self.fail
        self.registered[combo] = (cb, suppress, trigger_on_release)
        return f"handle-{combo}"

    def remove_hotkey(self, handle):
        self.removed.append(handle)


def test_hotkey_registers_and_calls_back_with_the_source():
    kb = FakeKeyboard()
    calls = []
    hk = GlobalHotkey("windows+alt+v", calls.append, keyboard_module=kb)
    assert hk.start() and hk.active and hk.start()
    cb, suppress, on_release = kb.registered["windows+alt+v"]
    assert on_release and not suppress and len(kb.registered) == 1
    cb()
    assert calls == ["hotkey"]
    hk.stop()
    assert kb.removed == ["handle-windows+alt+v"] and not hk.active


def test_hotkey_failure_is_reported_once_and_does_not_raise():
    errors = []
    hk = GlobalHotkey("windows+alt+v", lambda s: None, errors.append, keyboard_module=FakeKeyboard(fail=ImportError("no hook")))
    assert hk.start() is False and not hk.active
    assert len(errors) == 1 and "windows+alt+v" in errors[0] and "tray" in errors[0]


def test_hotkey_callback_errors_do_not_escape():
    kb = FakeKeyboard()
    hk = GlobalHotkey("x", lambda s: 1 / 0, keyboard_module=kb)
    hk.start()
    kb.registered["x"][0]()


def test_the_default_combo_is_valid_for_the_real_keyboard_library():
    import keyboard

    assert keyboard.parse_hotkey(config.settings["ui"]["hotkey"])
    assert config.settings["ui"]["hotkey"] == "windows+alt+v"


# ---- config persistence --------------------------------------------------------------------------------------------------
def write_cfg(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_set_toml_value_changes_in_place_and_keeps_comments(tmp_path):
    p = write_cfg(tmp_path, '# top comment\n[llm]\nmax_steps = 8   # steps\n\n[wakeword]\nenabled = true   # tray toggle\nthreshold = 0.5\n')
    config.set_toml_value("wakeword", "enabled", False, path=p)
    text = p.read_text(encoding="utf-8")
    assert "enabled = false   # tray toggle" in text and "# top comment" in text and "max_steps = 8   # steps" in text
    assert tomllib.loads(text)["wakeword"] == {"enabled": False, "threshold": 0.5}
    assert config.settings["wakeword"]["enabled"] is False
    config.set_toml_value("wakeword", "enabled", True, path=p)
    assert config.settings["wakeword"]["enabled"] is True


def test_set_toml_value_adds_missing_keys_and_sections(tmp_path):
    p = write_cfg(tmp_path, "[llm]\nmax_steps = 8\n\n[paths]\nvault = \"V\"\n")
    config.set_toml_value("llm", "failover_cooldown", 300, path=p)
    config.set_toml_value("wakeword", "model_path", 'models/hey "vex".onnx', path=p)
    config.set_toml_value("wakeword", "chime", True, path=p)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["llm"] == {"max_steps": 8, "failover_cooldown": 300} and data["paths"] == {"vault": "V"}
    assert data["wakeword"] == {"model_path": 'models/hey "vex".onnx', "chime": True}
    config.set_toml_value("wakeword", "chime", False, path=p)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["wakeword"]["chime"] is False
    assert not list(tmp_path.glob("*.tmp"))
    config.settings["wakeword"]["enabled"] = True


def test_set_toml_value_creates_the_file(tmp_path):
    p = tmp_path / "new.toml"
    config.set_toml_value("ui", "caption_seconds", 12, path=p)
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"ui": {"caption_seconds": 12}}
    config.settings["ui"]["caption_seconds"] = 8


# ---- create_file line endings ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,expect_crlf", [("run.bat", True), ("RUN.CMD", True), ("notes.txt", False), ("script.ps1", False)])
def test_create_file_line_endings(tmp_path, name, expect_crlf):
    from tools import registry
    import tools

    tools.load_all()
    f = tmp_path / name
    assert registry.execute("create_file", {"path": str(f), "content": "@echo off\ngoto end\n:end\r\necho done\n"}).ok
    raw = f.read_bytes()
    if expect_crlf:
        assert raw == b"@echo off\r\ngoto end\r\n:end\r\necho done\r\n"           # every line CRLF, none doubled
    else:
        assert raw == b"@echo off\ngoto end\n:end\r\necho done\n"                  # exactly what was given
