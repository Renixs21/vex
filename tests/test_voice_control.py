"""Live voice settings for the Control Center (ui/voice_control.py) and the per-turn latency rows."""
import json
import time
from types import SimpleNamespace

import pytest
from test_followup import make

from ui.api_core import ApiCore, ApiError
from ui.voice_control import VoiceControl

SETTINGS = {"voice": {"edge_voice": "en-US-AriaNeural", "edge_candidates": ["en-US-AriaNeural", "en-IN-NeerjaNeural"],
                      "followup_enabled": True, "followup_s": 8, "silence_ms": 700},
            "wakeword": {"enabled": True, "threshold": 0.24}}


class Wake:
    def __init__(self):
        self.threshold, self.label, self.running, self.last_level, self.last_score = 0.24, "Hey Vex", True, 0.31, 0.12
        self._paused = False

    @property
    def paused(self):                                     # a property, like voice.wakeword.WakeWordListener
        return self._paused


def setup(stack=None):
    c, *_ = make()
    saved = []
    edge = SimpleNamespace(name="edge", voice="en-US-AriaNeural", fallback=SimpleNamespace(voice="en-US-AriaNeural"))
    stack = stack or SimpleNamespace(chain=SimpleNamespace(providers=[edge, SimpleNamespace(name="windows")]),
                                     speaker=SimpleNamespace(player=SimpleNamespace(play=lambda *a: None)))
    vc = VoiceControl(c, wake=Wake(), stack=stack, set_value=lambda s, k, v: saved.append((s, k, v)), settings=json.loads(json.dumps(SETTINGS)))
    return vc, c, saved, edge


def test_get_reports_the_live_values():
    vc, c, saved, edge = setup()
    g = vc.get()
    assert g["wake"]["threshold"] == 0.24 and g["wake"]["label"] == "Hey Vex" and g["followup"]["seconds"] == c.followup_seconds
    assert g["voice"]["current"] == "en-US-AriaNeural" and "en-IN-NeerjaNeural" in g["voice"]["candidates"]


def test_levels_show_the_live_mic_and_wake_score():
    vc, *_ = setup()
    assert vc.levels() == {"level": 0.31, "score": 0.12, "threshold": 0.24, "listening": True}
    vc.wake._paused = True
    assert vc.levels()["listening"] is False


def test_set_changes_live_and_saves_to_config():
    vc, c, saved, edge = setup()
    vc.set({"threshold": 0.3, "followup_seconds": 5, "followup_enabled": False, "voice": "en-IN-NeerjaNeural"})
    assert vc.wake.threshold == 0.3 and c.followup_seconds == 5 and c.followup_enabled is False
    assert edge.voice == "en-IN-NeerjaNeural" and edge.fallback.voice == "en-IN-NeerjaNeural"
    assert ("wakeword", "threshold", 0.3) in saved and ("voice", "followup_s", 5) in saved and ("voice", "edge_voice", "en-IN-NeerjaNeural") in saved


@pytest.mark.parametrize("bad", [{"threshold": 0.01}, {"threshold": 2}, {"followup_seconds": 99}, {"voice": "evil-voice"}])
def test_out_of_range_or_unknown_values_are_refused(bad):
    vc, c, saved, edge = setup()
    with pytest.raises(ValueError):
        vc.set(bad)
    assert saved == []


def test_preview_only_when_idle_and_only_listed_voices(monkeypatch):
    played = []
    stack = SimpleNamespace(chain=SimpleNamespace(providers=[]), speaker=SimpleNamespace(player=SimpleNamespace(play=lambda *a: played.append(a))))
    vc, c, saved, edge = setup(stack)
    monkeypatch.setattr("voice.tts.EdgeTTS.synthesize", lambda self, text: SimpleNamespace(samples=[0.0], rate=24000))
    with pytest.raises(ValueError):
        vc.preview("not-a-voice")
    c.state = "speaking"
    with pytest.raises(ValueError):
        vc.preview("en-US-AriaNeural")
    c.state = "idle"
    assert vc.preview("en-US-AriaNeural") == {"previewing": "en-US-AriaNeural"}
    deadline = time.time() + 3
    while not played and time.time() < deadline:
        time.sleep(0.02)
    assert played


def test_api_voice_and_latency_turns(tmp_path):
    vc, c, saved, edge = setup()
    (tmp_path / "latency.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-10-04T18:00:00", "source": "hotkey", "outcome": "spoken", "stages": {"total": 4.4, "stt": 1.0, "brain": 1.5, "tts_first_audio": 0.6}},
        {"ts": "2026-10-04T18:01:00", "source": "followup", "outcome": "followup_expired", "stages": {}},
        {"ts": "2026-10-04T18:02:00", "source": "followup", "outcome": "spoken", "followup": True, "stages": {"total": 3.1}}]) + "\n",
        encoding="utf-8")
    core = ApiCore(controller=c, voice_control=vc, logs=tmp_path, data=tmp_path, latency_path=tmp_path / "latency.jsonl")
    rows = core.latency_turns({"last": 10})
    assert [r["total"] for r in rows] == [4.4, 3.1] and rows[1]["followup"] is True
    assert core.voice_get({})["wake"]["threshold"] == 0.24
    with pytest.raises(ApiError):
        core.voice_set({"threshold": 5})
    assert core.voice_set({"threshold": 0.3})["wake"]["threshold"] == 0.3
    with pytest.raises(ApiError):
        ApiCore(controller=c, logs=tmp_path, data=tmp_path).voice_get({})


def test_levels_work_with_the_real_listener_class():
    """Guard against fakes drifting from the real WakeWordListener (paused is a property there)."""
    from voice.wakeword import WakeWordListener

    assert isinstance(getattr(WakeWordListener, "paused"), property)
    w = WakeWordListener(lambda: "started", model_factory=lambda m, v: None)
    vc = VoiceControl(SimpleNamespace(state="idle", followup_enabled=True, followup_seconds=8), wake=w, stack=None,
                      set_value=lambda *a: None, settings=json.loads(json.dumps(SETTINGS)))
    assert vc.levels()["listening"] is False and vc.levels()["level"] == 0.0
