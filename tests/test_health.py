"""Health check: plain-word verdicts, no quota by default, never a key in the output, live checks only after asking."""
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ui import health
from ui.health import FAIL, PASS, SKIP, WARN, Check, Redactor

GROQ_KEY = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
GEM_KEY = "AIza" + "SyD-fakefakefakefakefake12345"
ENV = {"GROQ_API_KEY": GROQ_KEY, "GEMINI_API_KEY": GEM_KEY}
LLM = {"order": ["groq", "groq-small", "gemini"], "groq_model": "openai/gpt-oss-120b", "groq_small_model": "openai/gpt-oss-20b",
       "gemini_model": "gemini-flash"}


class HttpErr(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(f"{msg} key={GROQ_KEY}")              # providers sometimes echo things back: must never reach the report
        self.status_code = code


def probes(**over):
    calls = {"lookup": [], "tiny": []}

    def lookup(kind):
        return lambda key, model: calls["lookup"].append((kind, model))

    def tiny(kind):
        return lambda key, model: calls["tiny"].append((kind, model))

    p = {"env": lambda k: ENV.get(k, ""), "llm_lookup": {"groq": lookup("groq"), "gemini": lookup("gemini")},
         "llm_tiny": {"groq": tiny("groq"), "gemini": tiny("gemini")}, "stt_lookup": lambda k, m: calls["lookup"].append(("whisper", m)),
         "stt_transcribe": lambda k, m, a: calls["tiny"].append(("whisper", m)), "tts_synth": lambda t: b"", "windows_tts": lambda: True,
         "record": lambda: (np.random.default_rng(1).standard_normal(16000) * 400).astype(np.int16), "windows_muted": lambda: False,
         "wake_load": lambda m: object(), "pid_alive": lambda pid: True, "free_bytes": lambda p: 50e9, "size_mb": lambda p: 1.0}
    p.update(over)
    return p, calls


# ---- providers ---------------------------------------------------------------------------------------------------------------------------
def test_default_run_uses_free_lookups_only():
    p, calls = probes()
    out = health.check_llms(LLM, p["env"], False, p["llm_lookup"], p["llm_tiny"])
    assert [c.status for c in out] == [PASS] * 3 and calls["tiny"] == []
    assert [m for _, m in calls["lookup"]] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "gemini-flash"]
    assert "no quota used" in out[0].detail


def test_live_run_makes_one_tiny_request_per_provider():
    p, calls = probes()
    out = health.check_llms(LLM, p["env"], True, p["llm_lookup"], p["llm_tiny"])
    assert [c.status for c in out] == [PASS] * 3 and len(calls["tiny"]) == 3 and calls["lookup"] == []


@pytest.mark.parametrize("code,status,words", [(401, FAIL, "key was refused"), (404, FAIL, "doesn't exist"), (429, WARN, "rate-limited"),
                                                (None, FAIL, "couldn't reach")])
def test_provider_failures_are_explained_in_plain_words(code, status, words):
    def bad(key, model):
        raise HttpErr(code) if code else ConnectionError("offline")

    out = health.check_llms({**LLM, "order": ["groq"]}, lambda k: ENV.get(k, ""), False, {"groq": bad, "gemini": bad})
    assert out[0].status == status and words in out[0].detail and out[0].hint


def test_a_provider_without_a_key_is_skipped_not_failed():
    out = health.check_llms(LLM, lambda k: GROQ_KEY if k == "GROQ_API_KEY" else "", False,
                            {"groq": lambda k, m: None, "gemini": lambda k, m: None})
    assert [c.status for c in out] == [PASS, PASS, SKIP]


def test_groq_lookup_lists_models_once_and_handles_ids_with_slashes(monkeypatch):
    listed = []

    class FakeGroq:
        def __init__(self, **kw):
            self.models = SimpleNamespace(list=lambda: listed.append(1) or SimpleNamespace(
                data=[SimpleNamespace(id="openai/gpt-oss-120b"), SimpleNamespace(id="whisper-large-v3-turbo")]))

    import groq

    monkeypatch.setattr(groq, "Groq", FakeGroq)
    health._groq_models.clear()
    health.groq_lookup("k1", "openai/gpt-oss-120b")
    health.groq_lookup("k1", "whisper-large-v3-turbo")
    with pytest.raises(health.ModelMissing):
        health.groq_lookup("k1", "openai/gpt-oss-999b")
    assert listed == [1]
    health._groq_models.clear()


# ---- secrets ------------------------------------------------------------------------------------------------------------------------------
def test_the_report_never_contains_a_key_even_when_an_error_echoes_it():
    def bad(key, model):
        raise HttpErr(500, f"server said {key}")

    p, _ = probes(llm_lookup={"groq": bad, "gemini": bad})
    checks = health.check_llms(LLM, p["env"], False, p["llm_lookup"])
    text = health.render(checks, False, Redactor([GROQ_KEY, GEM_KEY]))
    assert GROQ_KEY not in text and GEM_KEY not in text and "[hidden]" in text


def test_the_redactor_also_catches_key_shaped_strings_it_was_not_told_about():
    r = Redactor([])
    assert "gsk_" not in r("x gsk_ZZZZZZZZZZZZZZZZZZZZ y") and "AIza" not in r("AIzaSyQQQQQQQQQQQQQQQQQQQQQQQQ")
    assert r("nothing secret here") == "nothing secret here"


def test_secrets_are_collected_from_the_environment_and_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOME_THING=abcdefgh123\nGROQ_API_KEY=gsk_fromfile123456\n", encoding="utf-8")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok_123456789")
    vals = health.secrets_from_env(tmp_path)
    assert "abcdefgh123" in vals and "gsk_fromfile123456" in vals and "tok_123456789" in vals


def test_keys_check_names_keys_but_never_shows_values():
    c = health.check_keys(lambda k: ENV.get(k, ""))
    assert c.status == PASS and "GROQ_API_KEY" in c.detail and GROQ_KEY not in c.detail
    assert health.check_keys(lambda k: "").status == FAIL
    assert health.check_keys(lambda k: GROQ_KEY if k == "GROQ_API_KEY" else "").status == WARN


# ---- speech, mic, wake word -------------------------------------------------------------------------------------------------------------------
def test_stt_free_by_default_and_live_only_when_asked():
    p, calls = probes()
    assert health.check_stt({"stt_model": "whisper-large-v3-turbo"}, p["env"], False, p["stt_lookup"], p["stt_transcribe"]).status == PASS
    assert calls == {"lookup": [("whisper", "whisper-large-v3-turbo")], "tiny": []}
    assert health.check_stt({"stt_model": "w"}, p["env"], True, p["stt_lookup"], p["stt_transcribe"]).status == PASS
    assert calls["tiny"] == [("whisper", "w")]
    assert health.check_stt({}, lambda k: "", False).status == FAIL


def test_tts_checks_edge_and_the_windows_voice():
    ok = health.check_tts("en-US-AriaNeural", lambda t: b"", lambda: True)
    assert [c.status for c in ok] == [PASS, PASS]

    def offline(t):
        raise OSError("no network")

    bad = health.check_tts("v", offline, lambda: False)
    assert [c.status for c in bad] == [WARN, FAIL] and "Windows voice" in bad[0].hint


def test_mic_states():
    loud = lambda: (np.ones(16000) * 300).astype(np.int16)  # noqa: E731
    quiet = lambda: (np.random.default_rng(0).standard_normal(16000) * 1.5).astype(np.int16)  # noqa: E731
    zeros = lambda: np.zeros(16000, np.int16)  # noqa: E731

    def broken():
        raise OSError("device unavailable")

    assert health.check_mic(loud, lambda: False).status == PASS
    assert health.check_mic(quiet, lambda: False).status == PASS and "quiet room" in health.check_mic(quiet, lambda: False).detail
    assert health.check_mic(zeros, lambda: False).status == FAIL
    assert health.check_mic(loud, lambda: True).status == FAIL and "MUTED" in health.check_mic(loud, lambda: True).detail
    assert health.check_mic(broken, lambda: False).status == FAIL


def test_wakeword_check(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "hey_vex.onnx").write_bytes(b"x")
    cfg = {"enabled": True, "model_path": "models/hey_vex.onnx", "fallback_model": "hey_jarvis", "threshold": 0.24}
    assert health.check_wakeword(cfg, tmp_path, lambda m: object()).status == PASS
    assert health.check_wakeword({**cfg, "model_path": "models/missing.onnx"}, tmp_path, lambda m: object()).status == WARN

    def boom(m):
        raise RuntimeError("onnx")

    assert health.check_wakeword(cfg, tmp_path, boom).status == FAIL
    assert health.check_wakeword({**cfg, "enabled": False}, tmp_path).status == SKIP


# ---- the running app ----------------------------------------------------------------------------------------------------------------------
def test_running_app_from_its_heartbeat():
    now = 1000.0
    hb = {"ts": now - 10, "pid": 123, "state": "idle", "hotkey": True, "wakeword": "running", "orb": "running", "mic_muted": False,
          "reminders": True, "followup": True}
    out = {c.name: c for c in health.check_running(hb, now, lambda p: True)}
    assert all(c.status == PASS for c in out.values()) and "follow-up on" in out["Listening"].detail
    assert health.check_running(None)[0].status == WARN
    assert health.check_running({**hb, "ts": now - 500}, now, lambda p: True)[0].status == WARN       # stale
    assert health.check_running(hb, now, lambda p: False)[0].status == WARN                          # the process is gone
    bad = {c.name: c.status for c in health.check_running({**hb, "hotkey": False, "wakeword": "down", "orb": "crashed", "reminders": False},
                                                           now, lambda p: True)}
    assert bad == {"Vex app": PASS, "Hotkey": FAIL, "Listening": FAIL, "Orb": FAIL, "Reminder scheduler": FAIL}
    assert {c.name: c.status for c in health.check_running({**hb, "mic_muted": True, "orb": "pill"}, now, lambda p: True)}["Listening"] == WARN


def test_heartbeat_round_trip(tmp_path):
    p = tmp_path / "data" / "status.json"
    health.write_heartbeat(p, {"hotkey": True})
    hb = health.read_heartbeat(p)
    assert hb["hotkey"] is True and hb["pid"] == os.getpid() and abs(hb["ts"] - time.time()) < 5
    assert health.read_heartbeat(tmp_path / "nope.json") is None


def test_app_status_reports_every_part():
    from ui.app import app_status

    alive = SimpleNamespace(is_alive=lambda: True)
    ctl = SimpleNamespace(state="idle", mic_muted=False, followup_enabled=True)
    tray = SimpleNamespace(actions={"wakeword": SimpleNamespace(isChecked=lambda: True)})
    wake = SimpleNamespace(_thread=alive, running=True)
    orb = SimpleNamespace(started_by_us=True, crashed=lambda: False)
    s = app_status(ctl, SimpleNamespace(active=True), wake, orb, tray)
    assert s["hotkey"] and s["wakeword"] == "running" and s["orb"] == "running" and s["followup"] and not s["mic_muted"]
    orb.watcher = SimpleNamespace(gave_up=True)
    assert app_status(ctl, SimpleNamespace(active=False), None, orb, tray)["orb"] == "pill"
    assert app_status(ctl, SimpleNamespace(active=False), SimpleNamespace(_thread=None, running=False), None, tray)["wakeword"] == "down"


# ---- memory, reminders, disk ---------------------------------------------------------------------------------------------------------------------
def test_memory_check(tmp_path):
    v = tmp_path / "VexVault"
    assert health.check_memory(v).status == FAIL
    (v / ".vexmind").mkdir(parents=True)
    (v / "profile.md").write_text("me", encoding="utf-8")
    assert health.check_memory(v).status == WARN                          # no index yet
    con = sqlite3.connect(v / ".vexmind" / "index.db")
    con.execute("create table t(x)")
    con.commit()
    con.close()
    c = health.check_memory(v)
    assert c.status == PASS and "1 notes" in c.detail
    (v / ".vexmind" / "index.db").write_bytes(b"not a database at all" * 100)
    assert health.check_memory(v).status == FAIL


def test_reminders_check(tmp_path):
    p = tmp_path / "reminders.json"
    assert health.check_reminders(p).status == PASS
    p.write_text(json.dumps([{"done": False}, {"done": True}]), encoding="utf-8")
    assert "1 pending, 1 done" in health.check_reminders(p).detail
    p.write_text("{broken", encoding="utf-8")
    assert health.check_reminders(p).status == FAIL


def test_disk_logs_and_vault_cap(tmp_path):
    sizes = {"logs": 5.0, "VexVault": 10.0}
    out = health.check_disk(tmp_path, tmp_path / "logs", tmp_path / "VexVault", lambda p: 50e9, lambda p: sizes[p.name])
    assert [c.status for c in out] == [PASS, PASS, PASS] and "5%" in out[2].detail
    sizes.update(logs=150.0, VexVault=170.0)
    out = health.check_disk(tmp_path, tmp_path / "logs", tmp_path / "VexVault", lambda p: 1e9, lambda p: sizes[p.name])
    assert [c.status for c in out] == [WARN, WARN, WARN] and "never deleted" in out[2].hint
    assert health.check_disk(tmp_path, tmp_path, tmp_path, lambda p: 1e8, lambda p: 0)[0].status == FAIL


# ---- whole run, report, CLI -------------------------------------------------------------------------------------------------------------------
def test_full_run_with_every_probe_faked(tmp_path):
    p, calls = probes()
    checks = health.run_checks(status={"ts": time.time(), "pid": os.getpid(), "hotkey": True, "wakeword": "running", "orb": "running",
                                       "reminders": True}, probes=p)
    names = [c.name for c in checks]
    assert names[0] == "API keys" and any(n.startswith("Brain #1") for n in names) and "Microphone" in names and "Start with Windows" in names
    assert calls["tiny"] == []                                            # the default run spent nothing


def test_a_crashing_check_becomes_a_finding_not_a_crash(monkeypatch):
    p, _ = probes()
    monkeypatch.setattr(health, "check_memory", lambda v: 1 / 0)
    checks = health.run_checks(status={}, probes=p)
    assert any(c.name == "memory" and c.status == FAIL and "ZeroDivisionError" in c.detail for c in checks)


def test_render_and_summary():
    checks = [Check("A", PASS, "fine"), Check("B", WARN, "meh", "do x"), Check("C", FAIL, "bad", "do y"), Check("D", SKIP, "n/a")]
    text = health.render(checks, False, Redactor([]))
    assert "no API quota used" in text and "-> do x" in text and "-> do y" in text and "1 passed, 1 warning(s), 1 failed, 1 skipped" in text
    assert health.summary_line(checks).startswith("1 problem(s): C")
    assert health.summary_line(checks[:2]).startswith("Working, 1 warning(s)") and health.summary_line(checks[:1]) == "Everything passed."


def test_cli_asks_before_spending_quota_and_falls_back_to_free(monkeypatch, capsys, tmp_path):
    seen = {}
    monkeypatch.setattr(health, "run_checks", lambda live=False, **k: seen.setdefault("live", []).append(live) or [Check("A", PASS, "ok")])
    monkeypatch.setattr(health, "write_report", lambda text, path: tmp_path / "health.txt")
    asked = []
    assert health.main(["--live"], ask=lambda q: asked.append(q) or "n") == 0
    assert seen["live"] == [False] and "tokens" in asked[0]
    assert health.main(["--live"], ask=lambda q: "y") == 0 and seen["live"][-1] is True
    assert health.main([], ask=lambda q: pytest.fail("the free run must not ask")) == 0 and seen["live"][-1] is False
    monkeypatch.setattr(health, "run_checks", lambda live=False, **k: [Check("A", FAIL, "bad")])
    assert health.main([], ask=lambda q: "") == 1


def test_tray_has_a_health_check_item_that_reports_back(qapp, tmp_path, monkeypatch):
    from test_ui_qt import FakeController, FakeExecutor, qapp as _q  # noqa: F401
    from ui.frontend import RecordingFrontend
    from ui.tray import TrayController

    c = FakeController()
    c.state = "idle"
    t = TrayController(qapp, c, RecordingFrontend(), FakeExecutor(), config_path=tmp_path / "c.toml")
    shown, opened = [], []
    monkeypatch.setattr(t, "notify", lambda title, msg, force=False: shown.append(msg))
    monkeypatch.setattr(t, "_open_path", opened.append)
    t.on_health = lambda: ("Everything passed.", str(tmp_path / "health.txt"))
    t.actions["health"].trigger()
    deadline = time.time() + 3
    while not opened and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert "Everything passed." in shown[-1] and opened and not t._health_running


from test_ui_qt import qapp  # noqa: E402,F401 - the shared QApplication fixture
