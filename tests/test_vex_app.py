"""The Control Center (vex_app): its API client against a real local API server, the view models, the QML loading cleanly, and the
rule that it never imports Vex's internals."""
import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_followup import make

pytest.importorskip("pytestqt")

from ui.api_core import ApiCore                                     # noqa: E402 - the test side may use Vex; the app may not
from ui.api_server import ApiServer                                  # noqa: E402
from vex_app.client import ApiClient                                 # noqa: E402
from vex_app.viewmodels import HomeModel, StatusModel                # noqa: E402

TOKEN = "z" * 43
APP_DIR = Path(__file__).resolve().parent.parent / "vex_app"


@pytest.fixture
def server(tmp_path):
    c, cap, sp, fe, states, ends = make()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "actions.jsonl").write_text(json.dumps({"ts": "2026-10-04T18:10:00+05:30", "tool": "open_app", "tier": "green",
                                                    "decision": "auto", "ok": True, "result": "Opened Notepad."}) + "\n", encoding="utf-8")
    core = ApiCore(controller=c, logs=logs, data=tmp_path, health_fn=lambda: {"summary": "Everything passed.", "checks": []},
                   status_fn=lambda: {"wakeword": "running", "hotkey": True}, hotkey="Ctrl+Alt+V")
    core.attach()
    srv = ApiServer(core, TOKEN, port=0)
    assert srv.start()
    yield SimpleNamespace(server=srv, core=core, controller=c, speaker=sp)
    srv.stop()


def connect(qtbot, port, token=TOKEN, retry_ms=200):
    client = ApiClient(port, lambda: token, retry_ms=retry_ms)
    client.start()
    return client


# ---- the client ------------------------------------------------------------------------------------------------------------------------
def test_client_connects_gets_state_and_calls(qtbot, server):
    client = connect(qtbot, server.server.bound_port)
    states = []
    client.event.connect(lambda name, data: name == "state" and states.append(data))
    qtbot.waitUntil(lambda: client.connected, timeout=5000)
    qtbot.waitUntil(lambda: bool(states), timeout=3000)
    assert states[0]["state"] == "idle" and states[0]["wakeword"] == "running"
    got = []
    client.call("budget", {}, lambda ok, data, err: got.append((ok, data, err)))
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    assert got[0][0] and got[0][1]["budget_inr"] == 1000
    client.call("nope", {}, lambda ok, data, err: got.append((ok, data, err)))
    qtbot.waitUntil(lambda: len(got) == 2, timeout=5000)
    assert got[1] == (False, None, "unknown command 'nope'")
    client.stop()


def test_client_with_a_wrong_token_never_connects(qtbot, server):
    client = connect(qtbot, server.server.bound_port, token="wrong", retry_ms=5000)
    qtbot.wait(1500)
    assert not client.connected
    client.stop()


def test_client_retries_until_vex_is_up_and_rereads_the_token(qtbot, server):
    tokens = [""]                                                          # no token file yet
    client = ApiClient(server.server.bound_port, lambda: tokens[0], retry_ms=150)
    client.start()
    qtbot.wait(400)
    assert not client.connected and client.attempts >= 2
    tokens[0] = TOKEN                                                      # Vex started and wrote its token
    qtbot.waitUntil(lambda: client.connected, timeout=5000)
    client.stop()


def test_calls_while_disconnected_fail_at_once(qtbot):
    client = ApiClient(1, lambda: "", retry_ms=10_000)
    got = []
    client.call("state", {}, lambda ok, d, e: got.append((ok, e)))
    assert got == [(False, "Vex isn't running")]


def test_client_pushes_events(qtbot, server):
    client = connect(qtbot, server.server.bound_port)
    qtbot.waitUntil(lambda: client.connected, timeout=5000)
    seen = []
    client.event.connect(lambda name, data: seen.append(name))
    server.controller.set_mic_muted(True)
    qtbot.waitUntil(lambda: "state" in seen, timeout=3000)
    server.controller.set_mic_muted(False)
    client.stop()


# ---- the status model ---------------------------------------------------------------------------------------------------------------------
def test_status_model_follows_vex(qtbot, server):
    client = connect(qtbot, server.server.bound_port)
    st = StatusModel(client)
    qtbot.waitUntil(lambda: st.connected and st.headline == "Listening for ‘Hey Vex’", timeout=5000)
    assert st.hotkey == "Ctrl+Alt+V" and st.state == "idle"
    st.setMuted(True)
    qtbot.waitUntil(lambda: st.muted, timeout=3000)
    assert st.headline == "Microphone muted" and server.controller.mic_muted
    st.setMuted(False)
    qtbot.waitUntil(lambda: not st.muted, timeout=3000)
    st.send("what's the time")
    qtbot.waitUntil(lambda: server.controller.agent.heard == ["what's the time"], timeout=5000)
    st.send("   ")                                                         # nothing sent
    client.stop()


def test_confirm_events_show_a_banner_and_clear(qtbot):
    client = ApiClient(1, lambda: "")
    st = StatusModel(client)
    st._on_event("confirm", {"tool": "delete_file", "tier": "red", "title": "Delete x.txt?"})
    assert st.confirm["tool"] == "delete_file"
    st._on_event("confirm_done", {"approved": False})
    assert st.confirm is None


def test_headline_and_uptime(qtbot):
    t = [100.0]
    client = SimpleNamespace(connected=True, event=SimpleNamespace(connect=lambda f: None),
                             connectedChanged=SimpleNamespace(connect=lambda f: None), call=lambda *a, **k: None)
    st = StatusModel(client, clock=lambda: t[0])
    st._apply({"state": "followup", "uptime_s": 3600 + 125})
    assert st.headline == "Still listening" and st.uptime == "1h 02m"
    t[0] += 86400
    assert st.uptime == "1d 1h"
    st._apply({"state": "idle", "wakeword": "off", "hotkey": "Ctrl+Alt+V"})
    assert st.headline == "Hotkey only (Ctrl+Alt+V)"


def test_start_vex_uses_the_launcher(qtbot):
    started = []
    st = StatusModel(ApiClient(1, lambda: ""), start_vex=lambda: started.append(1))
    st.startVex()
    assert started == [1]


# ---- the home model -------------------------------------------------------------------------------------------------------------------------
def test_home_model_loads_everything(qtbot, server):
    client = connect(qtbot, server.server.bound_port)
    home = HomeModel(client)
    qtbot.waitUntil(lambda: client.connected and bool(home.activity), timeout=5000)
    a = home.activity[0]
    assert a["title"] == "open_app" and a["mono"] and a["tier"] == "green" and "Opened Notepad." in a["detail"]
    qtbot.waitUntil(lambda: home.budget["total"] == 1000, timeout=3000)
    home.runHealth()
    qtbot.waitUntil(lambda: home.health["ran"], timeout=5000)
    assert home.health["summary"] == "Everything passed." and not home.healthBusy
    client.stop()


def test_activity_rows_are_described_plainly():
    d = HomeModel.describe
    assert d({"ts": "2026-10-04T18:39:28+05:30", "kind": "tool", "tool": "run_powershell", "tier": "red", "decision": "denied",
              "ok": False, "summary": ""}) == {"ts": "2026-10-04T18:39:28+05:30", "time": "18:39", "title": "run_powershell", "mono": True,
                                               "tier": "red", "detail": "declined", "ok": False}
    turn = d({"ts": "2026-10-04T18:40:00+05:30", "kind": "turn", "said": "what's the time", "reply": "It's 6:40."})
    assert turn["title"] == "You" and turn["detail"] == "what's the time" and not turn["mono"]
    assert d({"ts": "x", "kind": "confirm", "tool": "delete_file", "tier": "red", "approved": True})["detail"] == "approved"


def test_today_keeps_only_what_is_due_today_or_overdue(qtbot):
    home = HomeModel(ApiClient(1, lambda: ""))
    now = time.time()
    from datetime import datetime, timedelta

    soon = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0)
    later = soon + timedelta(days=3)
    if soon.date() != datetime.now().date():
        pytest.skip("too close to midnight")
    home._got_reminders(True, [{"text": "stretch", "due": soon.isoformat()}, {"text": "dentist", "due": later.isoformat()}], "")
    assert [t["text"] for t in home.today] == ["stretch"] and home.today[0]["time"] == soon.strftime("%H:%M")
    assert now


def test_budget_and_latency_views(qtbot):
    home = HomeModel(ApiClient(1, lambda: ""))
    home._budget = {"budget_inr": 1000, "used_inr": 250.0}
    home._latency = {"spoken": 12, "stages": {"total": {"median": 4.4}, "tts_first_audio": {"median": 0.6}}}
    assert home.budget["fraction"] == 0.25 and home.latency == {"median": 4.4, "firstAudio": 0.6, "turns": 12}


def test_new_activity_is_prepended_and_capped(qtbot):
    home = HomeModel(ApiClient(1, lambda: ""))
    for i in range(12):
        home._on_event("activity", {"ts": f"2026-10-04T18:{i:02d}:00", "kind": "tool", "tool": f"t{i}", "tier": "green", "ok": True})
    assert len(home.activity) == 10 and home.activity[0]["title"] == "t11"


# ---- the QML ----------------------------------------------------------------------------------------------------------------------------------
def test_the_qml_loads_without_errors_connected_and_offline(qtbot, server):
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine

    from vex_app import paths

    for port in (server.server.bound_port, 1):
        client = ApiClient(port, lambda: TOKEN, retry_ms=200)
        status, home = StatusModel(client), HomeModel(client)
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))
        ctx = engine.rootContext()
        ctx.setContextProperty("vex", status)
        ctx.setContextProperty("home", home)
        ctx.setContextProperty("appPaths", {"icons": QUrl.fromLocalFile(str(paths.ICONS) + "/").toString(),
                                            "shaders": QUrl.fromLocalFile(str(paths.SHADERS) + "/").toString(),
                                            "web": QUrl.fromLocalFile(str(paths.WEB) + "/").toString()})
        ctx.setContextProperty("startScreen", "home")
        engine.load(QUrl.fromLocalFile(str(paths.QML / "Main.qml")))
        assert engine.rootObjects(), warnings
        client.start()
        if port != 1:
            qtbot.waitUntil(lambda: client.connected, timeout=5000)
        qtbot.wait(300)
        client.stop()
        for o in engine.rootObjects():
            o.deleteLater()
        qtbot.wait(50)
        real = [w for w in warnings if "ShaderEffect" not in w and "shader" not in w.lower()]   # offscreen tests have no GPU
        assert real == [], real


# ---- the boundary -------------------------------------------------------------------------------------------------------------------------
VEX_PACKAGES = {"ui", "brain", "voice", "tools", "safety", "memory", "config", "main", "tools_dev"}


def test_the_app_never_imports_vex_internals():
    for py in APP_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for n in names:
                assert n.split(".")[0] not in VEX_PACKAGES, f"{py.name} imports {n}"


def test_fonts_and_vendor_libraries_are_bundled_with_licenses():
    assert (APP_DIR / "fonts" / "Geist-Variable.ttf").exists() and (APP_DIR / "fonts" / "GeistMono-Variable.ttf").exists()
    assert "Open Font License" in (APP_DIR / "fonts" / "OFL.txt").read_text(encoding="utf-8")
    assert (APP_DIR / "shaders" / "orb.frag.qsb").exists() and (APP_DIR / "shaders" / "panel.frag.qsb").exists()


def test_tray_and_pill_can_open_the_control_center(monkeypatch):
    import socket
    import threading

    from ui.orb_frontend import ClickListener

    opened, clicks = threading.Event(), []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cl = ClickListener(port, clicks.append, on_open=opened.set)
    cl.start()
    try:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(json.dumps({"event": "open_control_center"}).encode(), ("127.0.0.1", port))
        assert opened.wait(3) and clicks == []                             # opened, and not treated as a listen click
    finally:
        cl.stop()
