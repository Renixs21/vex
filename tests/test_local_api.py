"""The local API: 127.0.0.1 only, a token on every connection, the commands, the event stream, and no way to answer a confirmation."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_followup import make
from test_ui_core import FakeAgent

import tools
from helpers import NO, YES, ScriptedConfirmer
from safety.confirm import Confirmation, ConfirmRequest
from tools import registry
from ui.api_core import ApiCore, ApiError
from ui.api_server import ApiServer, load_or_create_token

TOKEN = "t" * 43


# ---- a running server on a free port -------------------------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path, mem):
    tools.load_all()
    c, cap, sp, fe, states, ends = make()
    logs, data = tmp_path / "logs", tmp_path / "data"
    logs.mkdir()
    data.mkdir()
    rows = [{"ts": "2026-10-04T18:00:00+05:30", "tool": "open_app", "tier": "green", "decision": "auto", "ok": True},
            {"ts": "2026-10-04T18:05:00+05:30", "tool": "delete_file", "tier": "red", "decision": "denied", "ok": False},
            {"ts": "2026-10-04T18:06:00+05:30", "tool": "open_app", "tier": "green", "decision": "auto", "ok": True}]
    (logs / "actions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    hk = SimpleNamespace(vault_bytes=lambda: 1_200_000.0, vault_cap_mb=200.0, vault_percent=lambda: 0.6, vault_full=lambda: False)
    executor = SimpleNamespace(listeners=[], undo=SimpleNamespace(peek=lambda: {"label": "Created notes.txt", "kind": "created",
                                                                                "audit_id": "a1"}),
                               run=lambda name, args: SimpleNamespace(ok=True, message=f"ran {name}"))
    dialog = ScriptedConfirmer()
    core = ApiCore(controller=c, executor=executor, memory_factory=lambda: mem, dialog_confirmer=dialog, housekeeper=hk,
                   health_fn=lambda: {"summary": "Everything passed.", "checks": []}, logs=logs, data=data)
    core.attach()
    server = ApiServer(core, TOKEN, port=0)
    assert server.start()
    yield SimpleNamespace(core=core, server=server, controller=c, capture=cap, speaker=sp, frontend=fe, dialog=dialog, executor=executor,
                          mem=mem, logs=logs, data=data)
    server.stop()


async def _session(port, token=TOKEN, header=True, first=None, origin=None, host=None):
    import aiohttp

    headers = {}
    if header and token:
        headers["Authorization"] = f"Bearer {token}"
    if origin:
        headers["Origin"] = origin
    if host:
        headers["Host"] = host
    s = aiohttp.ClientSession()
    ws = await s.ws_connect(f"ws://127.0.0.1:{port}/ws", headers=headers)
    if first is not None:
        await ws.send_json(first)
    return s, ws


def call(world, cmd, args=None, timeout=10):
    async def go():
        s, ws = await _session(world.server.bound_port)
        try:
            hello = await ws.receive_json(timeout=5)
            assert hello["event"] == "hello"
            await ws.send_json({"id": 7, "cmd": cmd, "args": args or {}})
            while True:
                m = await ws.receive_json(timeout=timeout)
                if m.get("id") == 7:
                    return m
        finally:
            await ws.close()
            await s.close()

    return asyncio.run(go())


# ---- security ------------------------------------------------------------------------------------------------------------------------------
def test_it_only_ever_binds_to_localhost():
    with pytest.raises(ValueError):
        ApiServer(SimpleNamespace(), TOKEN, host="0.0.0.0")


def test_no_token_wrong_token_or_late_token_is_refused(world):
    async def go():
        import aiohttp

        out = []
        for kw in ({"token": None}, {"token": "wrong"}, {"token": None, "header": False, "first": {"type": "auth", "token": "nope"}},
                   {"token": None, "header": False, "first": {"cmd": "state"}}):
            s, ws = await _session(world.server.bound_port, **kw)
            msg = await ws.receive(timeout=8)
            out.append((msg.type, ws.close_code))
            await s.close()
        return out

    results = asyncio.run(go())
    assert all(t in (__import__("aiohttp").WSMsgType.CLOSE, __import__("aiohttp").WSMsgType.CLOSED) and code == 4401 for t, code in results)


def test_a_wrong_header_token_is_refused_at_once(world):
    async def go():
        s, ws = await _session(world.server.bound_port, token="wrong")
        t0 = time.monotonic()
        await ws.receive(timeout=3)
        took = time.monotonic() - t0
        code = ws.close_code
        await s.close()
        return code, took

    code, took = asyncio.run(go())
    assert code == 4401 and took < 2.0                            # not the 5 s auth window


def test_the_token_can_also_come_as_the_first_message(world):
    async def go():
        s, ws = await _session(world.server.bound_port, header=False, first={"type": "auth", "token": TOKEN})
        hello = await ws.receive_json(timeout=5)
        await ws.close()
        await s.close()
        return hello

    assert asyncio.run(go())["event"] == "hello"


def test_browser_pages_and_foreign_hosts_are_refused(world):
    import aiohttp

    async def go(**kw):
        try:
            s, ws = await _session(world.server.bound_port, **kw)
            await s.close()
            return "connected"
        except aiohttp.WSServerHandshakeError as e:
            return e.status

    assert asyncio.run(go(origin="https://evil.example")) == 403
    assert asyncio.run(go(host="evil.example")) == 403


def test_repeated_bad_tokens_lock_new_connections_out(world):
    world.server._bad.extend([time.monotonic()] * 10)
    import aiohttp

    async def go():
        try:
            s, ws = await _session(world.server.bound_port)
            await s.close()
            return "connected"
        except aiohttp.WSServerHandshakeError as e:
            return e.status

    assert asyncio.run(go()) == 429


def test_the_token_file_is_created_once_and_strong(tmp_path):
    p = tmp_path / "data" / "api_token"
    a = load_or_create_token(p)
    assert len(a) >= 40 and load_or_create_token(p) == a and p.read_text(encoding="utf-8") == a


def test_the_token_is_never_logged(world, caplog):
    import logging

    caplog.set_level(logging.DEBUG)

    async def go():
        s, ws = await _session(world.server.bound_port, token="definitely-wrong-token-123")
        await ws.receive(timeout=8)
        await s.close()

    asyncio.run(go())
    call(world, "state")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text and "definitely-wrong-token-123" not in text and "refused a connection" in text


def test_the_token_file_is_gitignored():
    import subprocess

    from config import ROOT

    r = subprocess.run(["git", "check-ignore", "-q", str(ROOT / "data" / "api_token")], cwd=ROOT)
    assert r.returncode == 0


def test_there_is_no_command_to_answer_a_confirmation(world):
    names = set(world.core.commands)
    assert not any(w in n for n in names for w in ("confirm", "approve", "answer", "yes"))
    assert call(world, "confirm", {"approved": True})["error"] == "unknown command 'confirm'"


# ---- state, commands --------------------------------------------------------------------------------------------------------------------
def test_state_reports_the_six_states(world):
    c = world.controller
    assert call(world, "state")["data"]["state"] == "idle"
    c.mic_muted = True
    assert call(world, "state")["data"]["state"] == "muted"
    c.mic_muted = False
    c.asleep = True
    assert call(world, "state")["data"]["state"] == "asleep"
    c.asleep = False
    for raw in ("listening", "thinking", "speaking", "followup"):
        c.state = raw
        assert world.core.state({})["state"] == raw
    c.state = "idle"


def test_text_command_runs_a_turn(world):
    r = call(world, "text", {"text": "what's the time"})
    assert r["ok"] and r["data"]["result"] == "started"
    assert world.controller.wait_idle(5) and world.controller.agent.heard == ["what's the time"]
    assert call(world, "text", {"text": "   "})["error"] == "text is empty"


def test_mute_sleep_wake(world):
    assert call(world, "mute")["data"] == {"muted": True} and world.controller.mic_muted
    assert call(world, "unmute")["data"] == {"muted": False} and not world.controller.mic_muted
    assert call(world, "sleep")["data"]["asleep"] is True and ("hide", None) in world.frontend.calls
    assert call(world, "wake")["data"]["asleep"] is False and ("show", None) in world.frontend.calls
    assert call(world, "open_orb")["ok"]


def test_events_stream_state_tools_confirms_and_activity(world):
    async def go():
        s, ws = await _session(world.server.bound_port)
        await ws.receive_json(timeout=5)                          # hello
        got = []

        def later():
            time.sleep(0.2)
            world.controller.set_mic_muted(True)
            world.core._on_tool({"event": "tool_start", "tool": "open_app", "tier": "green"})
            world.core._on_tool({"event": "tool_end", "tool": "open_app", "tier": "green", "decision": "auto", "ok": True,
                                 "summary": "Opened notepad."})
            req = ConfirmRequest(tool="delete_file", tier="red", title="Delete?", detail="C:\\x.txt", requires_non_voice=True)
            world.core.on_confirm(req)
            world.core.on_confirm(req, Confirmation(False, "dialog", "declined"))

        threading.Thread(target=later).start()
        while len(got) < 7:
            got.append(await ws.receive_json(timeout=5))
        await ws.close()
        await s.close()
        return got

    events = asyncio.run(go())
    kinds = [e["event"] for e in events]
    assert "state" in kinds and kinds.count("tool") == 2 and "confirm" in kinds and "confirm_done" in kinds and "activity" in kinds
    conf = next(e for e in events if e["event"] == "confirm")["data"]
    assert conf["tool"] == "delete_file" and conf["on_screen_only"] is True
    assert world.core.state({})["confirm"] is None


def test_activity_and_audit(world):
    world.core._add_activity("turn", said="hi", reply="hello")
    assert call(world, "activity")["data"][-1]["said"] == "hi"
    rows = call(world, "audit", {"limit": 2})["data"]
    assert len(rows) == 2 and rows[-1]["tool"] == "open_app"
    assert [r["tool"] for r in call(world, "audit", {"tool": "delete_file"})["data"]] == ["delete_file"]


def test_undo_goes_through_the_executor(world):
    assert call(world, "undo.peek")["data"]["label"] == "Created notes.txt"
    assert call(world, "undo")["data"] == {"ok": True, "message": "ran undo_last"}


# ---- memory ------------------------------------------------------------------------------------------------------------------------------
def test_memory_search_note_neighbours_inbox_and_size(world):
    m = world.mem
    m.remember("Roommate since 2024", about="Rahul", note_type="person")
    m.remember("Portfolio site built with Next.js", about="Portfolio Site", note_type="project")
    m.remember("Rahul is helping with the [[Portfolio Site]]", about="Rahul")
    m.remember("Uses [[Tailwind]] and [[Vercel]]", about="Portfolio Site")
    m.add_to_inbox("Likes jazz (proposed for me, preference, confidence 0.70; personal)", source="curator")
    hits = call(world, "memory.search", {"query": "roommate"})["data"]
    assert hits and hits[0]["title"] == "Rahul"
    rahul = hits[0]["id"]
    note = call(world, "memory.note", {"id": rahul})["data"]
    assert note["title"] == "Rahul" and note["type"] == "person"
    assert call(world, "memory.note", {"name": "Rahul"})["data"]["id"] == rahul
    g = call(world, "memory.neighbours", {"id": rahul, "hops": 2})["data"]
    by = {n["title"]: n for n in g["nodes"]}
    assert by["Rahul"]["hop"] == 0 and by["Portfolio Site"]["hop"] == 1
    assert all({"id", "title", "type", "importance", "hop"} <= set(n) for n in g["nodes"])
    assert {"source": rahul, "target": by["Portfolio Site"]["id"]} in g["edges"]
    one = call(world, "memory.neighbours", {"id": rahul, "hops": 1})["data"]
    assert max(n["hop"] for n in one["nodes"]) == 1
    inbox = call(world, "memory.inbox")["data"]
    assert len(inbox) == 1
    item = inbox[0]
    r = call(world, "memory.discard", {"path": item["path"], "block": item["block"]})
    assert r["ok"] and call(world, "memory.inbox")["data"] == []
    assert call(world, "memory.size")["data"]["cap_mb"] == 200.0
    assert call(world, "memory.note", {"id": "nope"})["error"] == "no such note"


# ---- tools --------------------------------------------------------------------------------------------------------------------------------
def test_tools_list_has_tier_enabled_and_last_used(world):
    tl = {t["name"]: t for t in call(world, "tools.list")["data"]}
    assert tl["open_app"]["tier"] == "green" and tl["open_app"]["enabled"] and tl["open_app"]["last_used"].startswith("2026-10-04T18:06")
    assert tl["run_powershell"]["tier"] == "red" and tl["run_powershell"]["last_used"] is None


def test_disabling_a_tool_hides_it_from_the_brain_and_the_executor_refuses_it(world, tmp_path):
    from safety.audit import AuditLog
    from safety.executor import Executor

    assert call(world, "tools.disable", {"name": "open_app"})["data"] == {"name": "open_app", "enabled": False}
    assert "open_app" not in {t.name for t in registry.tools_for()}
    ex = Executor(ScriptedConfirmer(), AuditLog(tmp_path / "a.jsonl"), None)
    r = ex.run("open_app", {"name": "notepad"})
    assert not r.ok and "switched off" in r.message
    assert call(world, "tools.enable", {"name": "open_app"})["data"]["enabled"] is True and registry.is_enabled("open_app")


def test_a_red_tool_can_be_switched_off_but_only_back_on_with_an_on_screen_yes(world):
    assert call(world, "tools.disable", {"name": "run_powershell"})["ok"]
    world.dialog.answers = [NO]
    r = call(world, "tools.enable", {"name": "run_powershell"})
    assert not r["ok"] and "not switched on" in r["error"] and not registry.is_enabled("run_powershell")
    req = world.dialog.requests[-1]
    assert req.tier == "red" and req.requires_non_voice and "run_powershell" in req.title
    world.dialog.answers = [Confirmation(True, "voice", "")]                # a voice yes never counts
    assert not call(world, "tools.enable", {"name": "run_powershell"})["ok"]
    world.dialog.answers = [YES]
    assert call(world, "tools.enable", {"name": "run_powershell"})["data"]["enabled"] is True


def test_switched_off_tools_survive_a_restart(tmp_path):
    p = tmp_path / "off.json"
    registry.load_disabled(p)
    registry.set_enabled("shutdown_pc", False)
    registry._disabled.clear()
    assert "shutdown_pc" in registry.load_disabled(p)
    registry.set_enabled("shutdown_pc", True)


def test_nothing_switches_a_tool_back_on_by_itself(tmp_path):
    p = tmp_path / "off.json"
    registry.load_disabled(p)
    registry.set_enabled("delete_file", False)
    tools.load_all()                                                         # reloading tools / categories
    assert not registry.is_enabled("delete_file")
    registry.set_enabled("delete_file", True)


def test_unknown_tool(world):
    assert "no tool called" in call(world, "tools.disable", {"name": "nope"})["error"]


# ---- health, latency, autostart, budget -----------------------------------------------------------------------------------------------
def test_health_latency_autostart_budget(world, _autostart_never_touches_the_real_registry):
    assert call(world, "health")["data"]["summary"] == "Everything passed."
    (world.logs / "latency.jsonl").write_text(json.dumps({"ts": "2026-10-04T18:00:00", "source": "hotkey", "outcome": "spoken",
                                                          "stages": {"total": 3.0, "brain": 1.0}}) + "\n", encoding="utf-8")
    lat = call(world, "latency", {"last": 10})["data"]
    assert lat["spoken"] == 1 and lat["stages"]["total"]["median"] == 3.0
    assert call(world, "autostart.get")["data"]["on"] is False
    assert call(world, "autostart.set", {"on": True})["data"]["on"] is True
    assert call(world, "autostart.set", {"on": False})["data"] == {"on": False, "choice": "off"}
    b = call(world, "budget")["data"]
    assert b["budget_inr"] == 1000 and b["used_inr"] == 0 and b["left_inr"] == 1000 and "Nothing costs money" in b["note"]


def test_budget_counts_only_google_characters_past_the_free_allowance(world, monkeypatch):
    import config
    from voice.usage import UsageStore

    u = UsageStore(world.data / "voice_usage.json")
    u.add_month("google_chars", 1_100_000)
    monkeypatch.setitem(config.settings["voice"], "tts_order", ["google", "edge"])
    b = world.core.budget({})
    assert b["google_tts_on"] and b["google_chars_this_month"] == 1_100_000
    assert b["used_inr"] == pytest.approx(0.1 * 30 * 88) and b["left_inr"] == pytest.approx(1000 - 264)


def test_bad_requests_get_clear_errors(world):
    async def go():
        s, ws = await _session(world.server.bound_port)
        await ws.receive_json(timeout=5)
        await ws.send_str("not json")
        a = await ws.receive_json(timeout=5)
        await ws.send_json({"id": 1, "cmd": "nope"})
        b = await ws.receive_json(timeout=5)
        await ws.send_json({"id": 2, "cmd": "state", "args": [1]})
        c = await ws.receive_json(timeout=5)
        await ws.close()
        await s.close()
        return a, b, c

    a, b, c = asyncio.run(go())
    assert not a["ok"] and b["error"] == "unknown command 'nope'" and c["error"] == "args must be an object"


# ---- Vex is the same with no client ------------------------------------------------------------------------------------------------------
def test_with_no_client_events_go_nowhere_and_turns_run_normally(world, monkeypatch):
    scheduled = []
    monkeypatch.setattr("ui.api_server.asyncio.run_coroutine_threadsafe", lambda coro, loop: (scheduled.append(1), coro.close()))
    world.server._clients.clear()
    world.controller.submit_text("hello")                                   # a whole turn: state changes, activity, reply
    assert world.controller.wait_idle(5) and world.speaker.spoken == ["reply to hello"]
    world.controller.set_mic_muted(True)
    world.controller.set_mic_muted(False)
    assert scheduled == []                                                  # nothing was even queued for sending
    assert world.core.get_activity({})[-1]["kind"] == "turn"               # only the in-memory feed noted it


def test_vex_starts_without_the_api_when_the_port_is_taken(world):
    other = ApiServer(world.core, TOKEN, port=world.server.bound_port)
    assert other.start() is False and other.error


def test_the_cli_probe_talks_to_the_server(world, monkeypatch):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools_dev"))
    import api_probe

    out = []
    rc = asyncio.run(api_probe.run("state", {}, False, world.server.bound_port, TOKEN, out=out.append))
    assert rc == 0 and json.loads(out[-1])["state"] == "idle"
    rc = asyncio.run(api_probe.run("nope", {}, False, world.server.bound_port, TOKEN, out=out.append))
    assert rc == 1 and "unknown command" in out[-1]


def test_api_core_errors_are_api_errors(world):
    with pytest.raises(ApiError):
        world.core.memory_search({"query": ""})
    with pytest.raises(ApiError):
        world.core.autostart_set({})


def test_memory_graph_list_timeline_raw_save_forget(world, monkeypatch):
    m = world.mem
    m.remember("Roommate since 2024", about="Rahul", note_type="person")
    m.remember("Rahul is helping with the [[Portfolio Site]]", about="Rahul")
    m.remember("Portfolio site built with Next.js", about="Portfolio Site", note_type="project")
    g = call(world, "memory.graph")["data"]
    titles = {n["title"] for n in g["nodes"]}
    assert {"Rahul", "Portfolio Site"} <= titles and g["edges"]
    notes = call(world, "memory.list")["data"]
    rahul = next(n for n in notes if n["title"] == "Rahul")
    assert rahul["type"] == "person" and rahul["facts"] == 2
    tl = call(world, "memory.timeline")["data"]
    assert tl and any(i["title"] == "Rahul" and "Roommate" in i["text"] for d in tl for i in d["items"])
    raw = call(world, "memory.raw", {"id": rahul["id"]})["data"]["text"]
    assert "Roommate since 2024" in raw
    assert call(world, "memory.save", {"id": rahul["id"], "text": raw + "\n- My password is hunter2hunter2\n"})["ok"] is False
    assert call(world, "memory.save", {"id": rahul["id"], "text": raw.replace("Roommate since 2024", "Roommate since 2023")})["ok"]
    assert "2023" in call(world, "memory.raw", {"id": rahul["id"]})["data"]["text"]
    opened = []
    monkeypatch.setattr("os.startfile", opened.append, raising=False)
    assert call(world, "memory.open", {"id": rahul["id"]})["ok"] and opened and str(opened[0]).endswith(".md")
    prev = call(world, "memory.forget", {"id": rahul["id"]})["data"]
    assert prev["confirmed"] is False and "Rahul" in prev["message"]
    assert any(n["title"] == "Rahul" for n in call(world, "memory.list")["data"])          # a preview changes nothing
    trashed = []
    monkeypatch.setattr("memory.forget.send2trash", lambda p: trashed.append(p) or __import__("os").remove(p))
    done = call(world, "memory.forget", {"id": rahul["id"], "confirm": True})["data"]
    assert done["confirmed"] and done["ok"], done
    assert not any(n["title"] == "Rahul" for n in call(world, "memory.list")["data"])
    assert len(trashed) == 1 and trashed[0].endswith(".md")                 # went to the (fake) Recycle Bin
