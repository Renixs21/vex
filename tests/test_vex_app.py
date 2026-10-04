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
from vex_app.viewmodels import HomeModel, MemoryModel, StatusModel, ToolsModel   # noqa: E402

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
        ctx.setContextProperty("memory", MemoryModel(client))
        ctx.setContextProperty("tools", ToolsModel(client))
        ctx.setContextProperty("appPaths", {"icons": QUrl.fromLocalFile(str(paths.ICONS) + "/").toString(),
                                            "shaders": QUrl.fromLocalFile(str(paths.SHADERS) + "/").toString(),
                                            "web": QUrl.fromLocalFile(str(paths.WEB) + "/").toString()})
        ctx.setContextProperty("startScreen", "home")
        ctx.setContextProperty("startTab", 0)
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


# ---- the memory model --------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def mem_server(tmp_path, mem):
    c, *_ = make()
    mem.remember("Roommate since 2024", about="Rahul", note_type="person")
    mem.remember("Rahul is helping with the [[Portfolio Site]]", about="Rahul")
    mem.remember("Portfolio site built with Next.js and [[Tailwind]]", about="Portfolio Site", note_type="project")
    mem.add_to_inbox("Likes jazz (proposed for me, preference, confidence 0.70; personal)", source="curator")
    hk = SimpleNamespace(vault_bytes=lambda: 1_800_000.0, vault_cap_mb=200.0, vault_percent=lambda: 0.9, vault_full=lambda: False)
    core = ApiCore(controller=c, memory_factory=lambda: mem, housekeeper=hk, logs=tmp_path, data=tmp_path)
    core.attach()
    srv = ApiServer(core, TOKEN, port=0)
    assert srv.start()
    yield SimpleNamespace(server=srv, mem=mem)
    srv.stop()


def test_memory_model_starts_on_the_best_connected_note_and_loads_everything(qtbot, mem_server):
    client = connect(qtbot, mem_server.server.bound_port)
    m = MemoryModel(client)
    qtbot.waitUntil(lambda: bool(m.note.get("id")) and bool(m.focusGraph), timeout=8000)
    assert m.note["title"] in ("Rahul", "Portfolio Site")                  # the linked ones, not a lonely note
    assert {n["hop"] for n in m.focusGraph["nodes"]} >= {0, 1}
    assert m.size["text"] == "1.8 MB of 200 MB" and len(m.inbox) == 1 and "confidence 0.70" in m.inbox[0]["detail"]
    titles = {n["title"]: n for n in m.notes}
    assert titles["Rahul"]["typeLabel"] == "Person" and titles["Rahul"]["color"] == "#cdb98a"
    assert titles["Portfolio Site"]["color"] == "#93a8bf"
    m.loadGalaxy()
    qtbot.waitUntil(lambda: bool(m.galaxy.get("nodes")), timeout=5000)
    m.loadTimeline()
    qtbot.waitUntil(lambda: bool(m.timeline), timeout=5000)
    assert m.timeline[0]["items"] and m.timeline[0]["label"]
    m.search("roommate")
    qtbot.waitUntil(lambda: bool(m.results), timeout=5000)
    assert m.results[0]["title"] == "Rahul"
    client.stop()


def test_memory_model_selects_keeps_and_forgets_only_after_confirming(qtbot, mem_server):
    client = connect(qtbot, mem_server.server.bound_port)
    m = MemoryModel(client)
    qtbot.waitUntil(lambda: bool(m.notes), timeout=8000)
    rahul = next(n["id"] for n in m.notes if n["title"] == "Rahul")
    m.select(rahul)
    qtbot.waitUntil(lambda: m.note.get("id") == rahul, timeout=5000)
    assert any(f["text"] == "Rahul is helping with the Portfolio Site" for f in m.note["facts"])    # [[links]] shown as plain names
    assert any(l["title"] == "Portfolio Site" for l in m.note["links"])
    previews = []
    m.forgetPreview.connect(previews.append)
    m.askForget()
    qtbot.waitUntil(lambda: bool(previews), timeout=5000)
    assert "Rahul" in previews[0]
    assert any(n["title"] == "Rahul" for n in mem_server.mem.index.list_notes())               # the preview changed nothing
    m.confirmForget()
    qtbot.waitUntil(lambda: not any(n["title"] == "Rahul" for n in m.notes), timeout=5000)
    m.keep(0)
    qtbot.waitUntil(lambda: m.inbox == [], timeout=5000)
    client.stop()


def test_memory_model_edits_through_the_api_and_refuses_secrets(qtbot, mem_server):
    client = connect(qtbot, mem_server.server.bound_port)
    m = MemoryModel(client)
    toasts, raws = [], []
    m.toast.connect(toasts.append)
    m.rawLoaded.connect(raws.append)
    qtbot.waitUntil(lambda: bool(m.note.get("id")), timeout=8000)
    m.editNote()
    qtbot.waitUntil(lambda: bool(raws), timeout=5000)
    m.saveNote(raws[0] + "\n- my api key is sk-abcdefghijklmnopqrstuvwx123456\n")
    qtbot.waitUntil(lambda: bool(toasts), timeout=5000)
    assert "secret" in toasts[-1].lower()
    client.stop()


def test_body_and_link_cleanup():
    assert MemoryModel._unlink("See [[Aquarium]] and [[Rahul|my roommate]].") == "See Aquarium and my roommate."
    assert MemoryModel._plain_body("Facts about me.\n<!-- src: user -->\n- a fact ^b1\n## Notes") == "Facts about me."


def test_graph_page_is_local_only():
    html = (APP_DIR / "web" / "graph.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html                   # no CDN: everything from vendor/
    for f in ("sigma.min.js", "graphology.umd.min.js", "graphology-library.min.js"):
        assert f"vendor/{f}" in html and (APP_DIR / "web" / "vendor" / f).exists()


# ---- the tools model ------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def tools_server(tmp_path):
    import tools as tools_pkg
    from helpers import YES, ScriptedConfirmer

    tools_pkg.load_all()
    c, *_ = make()
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [{"id": "a1", "ts": "2026-10-04T18:10:00+05:30", "tool": "open_app", "tier": "green", "decision": "auto", "ok": True,
             "result": "Opened Notepad."},
            {"id": "a2", "ts": "2026-10-04T18:39:00+05:30", "tool": "run_powershell", "tier": "red", "decision": "denied", "ok": False,
             "reason": "action: declined", "via": {"action": "dialog"}},
            {"id": "a3", "ts": "2026-10-04T18:40:00+05:30", "tool": "create_file", "tier": "green", "decision": "auto", "ok": True,
             "result": "Created notes.txt", "undoable": True}]
    (logs / "actions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    undone = []
    executor = SimpleNamespace(listeners=[], undo=SimpleNamespace(peek=lambda: None if undone else
                                                                  {"audit_id": "a3", "kind": "created", "label": "Created notes.txt"}),
                               run=lambda name, args: undone.append(name) or SimpleNamespace(ok=True, message="Removed notes.txt."))
    dialog = ScriptedConfirmer()
    core = ApiCore(controller=c, executor=executor, dialog_confirmer=dialog, logs=logs, data=tmp_path)
    core.attach()
    srv = ApiServer(core, TOKEN, port=0)
    assert srv.start()
    yield SimpleNamespace(server=srv, dialog=dialog, yes=YES, undone=undone)
    srv.stop()


def test_tools_model_lists_filters_and_counts(qtbot, tools_server):
    client = connect(qtbot, tools_server.server.bound_port)
    t = ToolsModel(client)
    qtbot.waitUntil(lambda: len(t.tools) > 30, timeout=8000)
    names = {x["name"]: x for x in t.tools}
    assert names["open_app"]["tier"] == "green" and names["open_app"]["lastUsed"] != "never"
    assert names["run_powershell"]["tier"] == "red" and names["run_powershell"]["enabled"]
    assert t.counts["red"] >= 1 and t.counts["all"] == len(t.tools)
    t.setTier("red")
    assert t.tools and all(x["tier"] == "red" for x in t.tools)
    t.setTier("all")
    t.setQuery("clipboard")
    assert {x["name"] for x in t.tools} == {"get_clipboard", "set_clipboard"}
    qtbot.waitUntil(lambda: len(t.audit) == 3, timeout=5000)
    audit = t.audit
    assert audit[0]["tool"] == "create_file" and audit[1]["decision"] == "declined" and audit[1]["via"] == "dialog"
    qtbot.waitUntil(lambda: t.undoInfo["available"], timeout=5000)        # a separate request: may arrive after the list
    assert t.undoInfo == {"available": True, "auditId": "a3", "label": "Created notes.txt", "verb": "Undo"}
    client.stop()


def test_tools_model_switches_off_and_red_back_on_only_with_the_on_screen_yes(qtbot, tools_server):
    from tools import registry

    client = connect(qtbot, tools_server.server.bound_port)
    t = ToolsModel(client)
    toasts = []
    t.toast.connect(toasts.append)
    qtbot.waitUntil(lambda: len(t.tools) > 30, timeout=8000)
    t.setEnabled("run_powershell", False)
    qtbot.waitUntil(lambda: not registry.is_enabled("run_powershell"), timeout=5000)
    qtbot.waitUntil(lambda: not next(x for x in t.tools if x["name"] == "run_powershell")["enabled"], timeout=5000)
    tools_server.dialog.answers = [tools_server.yes]
    t.setEnabled("run_powershell", True)
    assert any("Confirm on Vex's screen" in m for m in toasts)
    assert next(x for x in t.tools if x["name"] == "run_powershell")["pending"]
    qtbot.waitUntil(lambda: registry.is_enabled("run_powershell"), timeout=5000)
    assert tools_server.dialog.requests[-1].tier == "red"
    client.stop()


def test_tools_model_undo_goes_through_vex(qtbot, tools_server):
    client = connect(qtbot, tools_server.server.bound_port)
    t = ToolsModel(client)
    toasts = []
    t.toast.connect(toasts.append)
    qtbot.waitUntil(lambda: t.undoInfo["available"], timeout=8000)
    t.undo()
    qtbot.waitUntil(lambda: bool(toasts), timeout=5000)
    assert tools_server.undone == ["undo_last"] and toasts[-1] == "Removed notes.txt."
    qtbot.waitUntil(lambda: not t.undoInfo["available"], timeout=5000)
    client.stop()


def test_restore_is_the_word_for_an_undone_delete(qtbot):
    t = ToolsModel(ApiClient(1, lambda: ""))
    t._undo = {"audit_id": "x", "kind": "deleted", "label": "Deleted old.txt"}
    assert t.undoInfo["verb"] == "Restore"
