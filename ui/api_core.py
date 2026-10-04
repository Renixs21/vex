"""What the local API can see and do: every command is a plain function here; ui/api_server.py only moves JSON.

Kept deliberately narrow:
  * Commands do what the tray or a typed command can already do. Nothing here can approve a confirmation: confirmations still happen
    on screen (and, for yellow ones, by voice) in Vex itself. A red tool can be switched off from here, but switching one back on
    asks for an on-screen confirmation in Vex.
  * Everything is read-only except: text commands, mute, sleep/wake/show, undo (through the normal safety pipeline), the memory inbox
    (keep/discard), switching tools on/off, and autostart.
  * With no client connected nothing changes: events go into a small in-memory buffer and nowhere else.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("vex.api")

ACTIVITY_MAX = 200
MAX_TEXT = 2000
STATES = ("listening", "thinking", "speaking", "followup", "muted", "asleep", "idle")


class ApiError(Exception):
    """A command failed in a way the client should see (bad arguments, not found, not allowed)."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ApiCore:
    def __init__(self, *, controller, executor=None, tray=None, memory_factory: Callable[[], Any] | None = None,
                 dialog_confirmer=None, housekeeper=None, health_fn: Callable[[], dict] | None = None,
                 logs: Path | None = None, data: Path | None = None, budget_cfg: dict | None = None,
                 latency_path: Path | None = None, status_fn: Callable[[], dict] | None = None, hotkey: str = "", conversations=None,
                 voice_control=None, restart_fn: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.controller = controller
        self.executor = executor
        self.tray = tray
        self.memory_factory = memory_factory
        self.dialog_confirmer = dialog_confirmer        # the on-screen dialog: the only way a red tool is switched back on
        self.housekeeper = housekeeper
        self.health_fn = health_fn
        self.status_fn = status_fn                      # the app's live status (wake word, orb, hotkey...): app_status()
        self.conversations = conversations              # ui.conversations.ConversationLog (None: no history)
        self.voice = voice_control                      # ui.voice_control.VoiceControl (None: no voice settings)
        self.restart_fn = restart_fn                    # quits Vex with the restart code (the supervisor starts it again)
        self.hotkey = hotkey
        self._clock = clock
        self.started_at = clock()
        self.last_health: dict | None = None
        from config import ROOT, settings

        self.logs = logs or ROOT / settings["paths"]["logs"]
        self.data = data or ROOT / "data"
        self.latency_path = latency_path or self.logs / "latency.jsonl"
        self.budget_cfg = budget_cfg or settings.get("budget", {})
        self.activity: deque[dict] = deque(maxlen=ACTIVITY_MAX)
        self.current_tool: dict | None = None
        self.pending_confirm: dict | None = None
        self.subscribers: list[Callable[[dict], None]] = []      # the server's broadcast; empty = nobody listening
        self._lock = threading.Lock()
        self.commands: dict[str, Callable[[dict], Any]] = {
            "state": self.state, "text": self.text, "listen": self.listen, "mute": self.mute, "unmute": self.unmute,
            "sleep": self.sleep, "wake": self.wake, "open_orb": self.open_orb,
            "activity": self.get_activity, "audit": self.audit, "undo.peek": self.undo_peek, "undo": self.undo,
            "memory.search": self.memory_search, "memory.note": self.memory_note, "memory.neighbours": self.memory_neighbours,
            "memory.inbox": self.memory_inbox, "memory.keep": self.memory_keep, "memory.discard": self.memory_discard,
            "memory.size": self.memory_size, "memory.graph": self.memory_graph, "memory.list": self.memory_list,
            "memory.timeline": self.memory_timeline, "memory.raw": self.memory_raw, "memory.save": self.memory_save,
            "memory.open": self.memory_open, "memory.forget": self.memory_forget,
            "tools.list": self.tools_list, "tools.enable": self.tools_enable, "tools.disable": self.tools_disable,
            "health": self.health, "latency": self.latency, "autostart.get": self.autostart_get, "autostart.set": self.autostart_set,
            "budget": self.budget, "commands": lambda a: sorted(self.commands),
            "health.last": self.health_last, "reminders.list": self.reminders_list, "tasks.list": self.tasks_list,
            "conversations.list": self.conversations_list, "conversations.get": self.conversations_get,
            "tasks.add": self.tasks_add, "tasks.complete": self.tasks_complete, "tasks.delete": self.tasks_delete,
            "reminders.add": self.reminders_add, "reminders.cancel": self.reminders_cancel,
            "voice.get": self.voice_get, "voice.set": self.voice_set, "voice.levels": self.voice_levels, "voice.preview": self.voice_preview,
            "latency.turns": self.latency_turns,
            "system.info": self.system_info, "system.restart": self.system_restart, "budget.models": self.budget_models,
            "logs.list": self.logs_list, "logs.tail": self.logs_tail, "logs.open": self.logs_open,
        }

    # ---- wiring (called by the app once) ------------------------------------------------------------------------------------
    def attach(self) -> None:
        c = self.controller
        c.listeners.append(lambda st: self._changed("state"))
        c.on_mute_change.append(lambda: self._changed("mute"))
        c.on_park_change.append(lambda: self._changed("asleep"))
        c.on_turn.append(lambda said, reply: self._add_activity("turn", said=said[:MAX_TEXT], reply=(reply or "")[:MAX_TEXT]))
        if self.executor is not None:
            self.executor.listeners.append(self._on_tool)
        if self.conversations is not None:
            from voice.timeline import timeline

            cv = self.conversations
            c.on_interaction_start.append(cv.start_session)
            c.on_turn.append(cv.add_turn)
            c.on_interaction_end.append(cv.end_session)
            timeline.listeners.append(cv.add_timing)
            if self.executor is not None:
                self.executor.listeners.append(cv.add_tool)

    def on_confirm(self, req, answer=None) -> None:
        """Called by the app's confirmation tracker: a prompt is showing (answer None) or was answered. Display only."""
        if answer is None:
            self.pending_confirm = {"tool": req.tool, "tier": req.tier, "title": req.title, "detail": req.detail[:500],
                                    "on_screen_only": bool(req.requires_non_voice), "since": _now()}
            self._publish({"event": "confirm", "data": self.pending_confirm})
        else:
            done = {**(self.pending_confirm or {"tool": req.tool}), "approved": bool(answer.approved), "via": answer.channel}
            self.pending_confirm = None
            self._publish({"event": "confirm_done", "data": done})
            self._add_activity("confirm", tool=req.tool, tier=req.tier, approved=bool(answer.approved), via=answer.channel)

    def _on_tool(self, ev: dict) -> None:
        if ev["event"] == "tool_start":
            self.current_tool = {"tool": ev["tool"], "tier": ev["tier"], "since": _now()}
            self._publish({"event": "tool", "data": {**self.current_tool, "running": True}})
        else:
            self.current_tool = None
            self._publish({"event": "tool", "data": {k: ev[k] for k in ("tool", "tier", "decision", "ok", "summary")} | {"running": False}})
            self._add_activity("tool", tool=ev["tool"], tier=ev["tier"], decision=ev["decision"], ok=ev["ok"], summary=ev["summary"])

    def _changed(self, what: str) -> None:
        self._publish({"event": "state", "data": self.state({})})

    def _add_activity(self, kind: str, **data) -> None:
        item = {"ts": _now(), "kind": kind, **data}
        with self._lock:
            self.activity.append(item)
        self._publish({"event": "activity", "data": item})

    def _publish(self, event: dict) -> None:
        for fn in list(self.subscribers):
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - a client problem never touches Vex
                log.debug("api publish failed", exc_info=True)

    # ---- state + commands ------------------------------------------------------------------------------------------------------
    def state(self, args: dict) -> dict:
        c = self.controller
        raw = c.state
        if c.mic_muted:
            shown = "muted"
        elif raw == "idle" and getattr(c, "asleep", False):
            shown = "asleep"
        else:
            shown = raw if raw in STATES else "idle"
        extra: dict = {}
        if self.status_fn is not None:
            try:
                extra = self.status_fn() or {}
            except Exception:  # noqa: BLE001
                extra = {}
        return {"state": shown, "controller_state": raw, "muted": bool(c.mic_muted), "asleep": bool(getattr(c, "asleep", False)),
                "followup_open": raw == "followup", "followup_enabled": bool(getattr(c, "followup_enabled", False)),
                "followup_seconds": float(getattr(c, "followup_seconds", 0) or 0),
                "wakeword": extra.get("wakeword", "off"), "hotkey": self.hotkey, "hotkey_ok": bool(extra.get("hotkey", False)),
                "orb": extra.get("orb"), "uptime_s": int(self._clock() - self.started_at),
                "current_tool": self.current_tool, "confirm": self.pending_confirm}

    def text(self, args: dict) -> dict:
        t = " ".join(str(args.get("text", "")).split())
        if not t:
            raise ApiError("text is empty")
        if len(t) > MAX_TEXT:
            raise ApiError(f"text is longer than {MAX_TEXT} characters")
        self._add_activity("command", via="api", text=t)
        return {"result": self.controller.submit_text(t, source="api")}

    def listen(self, args: dict) -> dict:
        return {"result": self.controller.start_listening("api")}

    def mute(self, args: dict) -> dict:
        on = bool(args.get("on", True))
        self.controller.set_mic_muted(on)
        return {"muted": on}

    def unmute(self, args: dict) -> dict:
        return self.mute({"on": False})

    def sleep(self, args: dict) -> dict:
        self.controller.park()
        return {"asleep": bool(self.controller.asleep)}

    def wake(self, args: dict) -> dict:
        self.controller.unpark()
        return {"asleep": bool(self.controller.asleep)}

    def open_orb(self, args: dict) -> dict:
        return self.wake(args)

    # ---- activity, audit, undo ---------------------------------------------------------------------------------------------------
    def get_activity(self, args: dict) -> list[dict]:
        n = max(1, min(ACTIVITY_MAX, int(args.get("limit", 50))))
        with self._lock:
            return list(self.activity)[-n:]

    def _audit_rows(self) -> list[dict]:
        path = self.logs / "actions.jsonl"
        rows = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return rows

    def audit(self, args: dict) -> list[dict]:
        n = max(1, min(500, int(args.get("limit", 50))))
        tool = args.get("tool")
        rows = [r for r in self._audit_rows() if not tool or r.get("tool") == tool]
        return rows[-n:]

    def undo_peek(self, args: dict) -> dict | None:
        undo = getattr(self.executor, "undo", None)
        top = undo.peek() if undo is not None else None
        return None if top is None else {"label": top.get("label"), "kind": top.get("kind"), "audit_id": top.get("audit_id")}

    def undo(self, args: dict) -> dict:
        """Undo the last file action through the normal safety pipeline (audited like any tool call)."""
        if self.executor is None:
            raise ApiError("undo isn't available")
        r = self.executor.run("undo_last", {})
        return {"ok": r.ok, "message": r.message}

    # ---- memory --------------------------------------------------------------------------------------------------------------------
    def _viewer(self):
        if self.memory_factory is None:
            raise ApiError("memory isn't available")
        from memory.viewer_service import ViewerService

        return ViewerService(self.memory_factory())

    def memory_search(self, args: dict) -> list[dict]:
        q = str(args.get("query", "")).strip()
        if not q:
            raise ApiError("query is empty")
        return self._viewer().search(q, limit=max(1, min(50, int(args.get("limit", 20)))))

    def memory_note(self, args: dict) -> dict:
        v = self._viewer()
        nid = str(args.get("id", "")) or (v.resolve(str(args.get("name", ""))) or "")
        note = v.note(nid) if nid else None
        if note is None:
            raise ApiError("no such note")
        return note

    def memory_neighbours(self, args: dict) -> dict:
        """The note and its neighbours up to `hops` (1 or 2) links away, both directions: nodes (type, importance, hop) and edges."""
        v = self._viewer()
        nid = str(args.get("id", "")) or (v.resolve(str(args.get("name", ""))) or "")
        hops = max(1, min(2, int(args.get("hops", 2))))
        index = v.index
        index.sync()
        if not nid or index.note(nid) is None:
            raise ApiError("no such note")
        seen = {nid: 0}
        edges: set[tuple[str, str]] = set()
        frontier = [nid]
        for hop in range(1, hops + 1):
            nxt = []
            for cur in frontier:
                outs, ins = index.neighbors(cur)
                for o in outs:
                    edges.add((cur, o))
                    if o not in seen:
                        seen[o] = hop
                        nxt.append(o)
                for i in ins:
                    edges.add((i, cur))
                    if i not in seen:
                        seen[i] = hop
                        nxt.append(i)
            frontier = nxt
        nodes = []
        for node_id, hop in seen.items():
            row = index.note(node_id)
            if row is None or row["type"] == "inbox":
                continue
            nodes.append({"id": node_id, "title": row["title"], "type": row["type"], "importance": row["importance"], "hop": hop})
        ids = {n["id"] for n in nodes}
        return {"center": nid, "hops": hops, "nodes": nodes,
                "edges": [{"source": a, "target": b} for a, b in sorted(edges) if a in ids and b in ids]}

    def memory_inbox(self, args: dict) -> list[dict]:
        return self._viewer().inbox()

    def memory_keep(self, args: dict) -> dict:
        path, block = str(args.get("path", "")), str(args.get("block", ""))
        if not path or not block:
            raise ApiError("path and block are required (from memory.inbox)")
        r = self._viewer().approve(path, block, str(args.get("note", "")), str(args.get("type", "")))
        self._add_activity("memory", action="keep", block=block)
        return r

    def memory_discard(self, args: dict) -> dict:
        path, block = str(args.get("path", "")), str(args.get("block", ""))
        if not path or not block:
            raise ApiError("path and block are required (from memory.inbox)")
        r = self._viewer().discard(path, block)
        self._add_activity("memory", action="discard", block=block)
        return r

    def memory_graph(self, args: dict) -> dict:
        """Every note and link (the Galaxy view): nodes (id, title, type, importance, links) and edges."""
        return self._viewer().graph(show_daily=bool(args.get("daily", False)))

    def memory_list(self, args: dict) -> list[dict]:
        """The notes, newest change first (the Notes cards): id, title, type, importance, updated, facts count."""
        v = self._viewer()
        v.index.sync()
        with v.index.lock:
            counts = dict(v.index.db.execute("SELECT note_id, COUNT(*) FROM blocks GROUP BY note_id").fetchall())
        rows = [r for r in v.index.list_notes(args.get("type") or None) if r["type"] != "inbox"]
        out = [{"id": r["id"], "title": r["title"], "type": r["type"], "importance": r["importance"], "updated": r["updated"] or "",
                "facts": int(counts.get(r["id"], 0))} for r in rows]
        out.sort(key=lambda n: n["updated"], reverse=True)
        return out

    def memory_timeline(self, args: dict) -> list[dict]:
        """What Vex learned, newest day first: [{day, items: [{note_id, title, type, text, source, learned}]}]."""
        from memory import parser

        v = self._viewer()
        v.index.sync()
        limit = max(1, min(1000, int(args.get("limit", 300))))
        with v.index.lock:
            rows = v.index.db.execute(
                "SELECT b.note_id, b.text, b.learned, b.source, n.title, n.type FROM blocks b JOIN notes n ON n.id=b.note_id "
                "WHERE b.learned IS NOT NULL AND b.learned != '' AND n.type != 'inbox' ORDER BY b.learned DESC LIMIT ?", (limit,)).fetchall()
        days: dict[str, list] = {}
        for note_id, text, learned, source, title, type_ in rows:
            days.setdefault(str(learned)[:10], []).append({"note_id": note_id, "title": title, "type": type_, "learned": learned,
                                                            "source": source or "", "text": parser.strip_links(text or "")[:300]})
        return [{"day": d, "items": items} for d, items in days.items()]

    def _note_id(self, v, args: dict) -> str:
        nid = str(args.get("id", "")) or (v.resolve(str(args.get("name", ""))) or "")
        if not nid or v.index.note(nid) is None:
            raise ApiError("no such note")
        return nid

    def memory_raw(self, args: dict) -> dict:
        v = self._viewer()
        nid = self._note_id(v, args)
        return {"id": nid, "text": v.raw(nid) or ""}

    def memory_save(self, args: dict) -> dict:
        """Save an edited note (the whole markdown file). Refuses secrets and a changed id line, like Vex's own viewer."""
        v = self._viewer()
        nid = self._note_id(v, args)
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError("text is empty")
        r = v.save(nid, text)
        if not r.get("ok"):
            raise ApiError(r.get("error") or "not saved")
        self._add_activity("memory", action="edit", note=nid)
        return r

    def memory_open(self, args: dict) -> dict:
        """Open the note's markdown file in the default editor on this PC."""
        import os

        v = self._viewer()
        nid = self._note_id(v, args)
        path = v.index.path_of(nid)
        try:
            os.startfile(path)  # noqa: S606
        except OSError as e:
            raise ApiError(f"couldn't open it: {e}") from e
        return {"opened": str(path)}

    def memory_forget(self, args: dict) -> dict:
        """Forget a whole note. Without confirm: the preview only (what would go). With confirm=true: the note goes to the Recycle Bin,
        facts mainly about it are deleted and other facts lose the name (exactly Vex's own "forget")."""
        v = self._viewer()
        nid = self._note_id(v, args)
        title = v.index.note(nid)["title"]
        r = self.memory_factory().forget(title, confirm=bool(args.get("confirm", False)))
        if args.get("confirm"):
            self._add_activity("memory", action="forget", note=title)
        return {"ok": r.ok, "message": r.message, "confirmed": bool(args.get("confirm", False))}

    def memory_size(self, args: dict) -> dict:
        hk = self.housekeeper
        if hk is None:
            raise ApiError("vault size isn't available")
        used = hk.vault_bytes()
        return {"bytes": int(used), "mb": round(used / 1e6, 2), "cap_mb": hk.vault_cap_mb, "percent": round(hk.vault_percent(), 1),
                "automatic_entries_paused": hk.vault_full()}

    # ---- tools ------------------------------------------------------------------------------------------------------------------------
    def _last_used(self) -> dict[str, str]:
        last: dict[str, str] = {}
        for r in self._audit_rows():
            t, ts = r.get("tool"), r.get("ts") or r.get("time")
            if t and ts:
                last[t] = ts
        return last

    def tools_list(self, args: dict) -> list[dict]:
        from tools import registry

        last = self._last_used()
        return [{"name": t.name, "tier": t.tier, "category": t.category, "enabled": registry.is_enabled(t.name),
                 "description": t.description, "last_used": last.get(t.name)} for t in registry.all_tools()]

    def _tool(self, args: dict):
        from tools import registry

        name = str(args.get("name", ""))
        t = registry.get(name)
        if t is None:
            raise ApiError(f"no tool called {name!r}")
        return t

    def tools_disable(self, args: dict) -> dict:
        from tools import registry

        t = self._tool(args)
        registry.set_enabled(t.name, False)
        log.info("tool %s switched off (via the local API)", t.name)
        self._add_activity("tools", action="disable", tool=t.name, tier=t.tier)
        return {"name": t.name, "enabled": False}

    def tools_enable(self, args: dict) -> dict:
        """Green/yellow: switched on. Red: only after you say yes in the on-screen dialog in Vex (never by voice, never from here)."""
        from tools import registry

        t = self._tool(args)
        if registry.is_enabled(t.name):
            return {"name": t.name, "enabled": True}
        if t.tier == "red":
            if self.dialog_confirmer is None:
                raise ApiError(f"{t.name} is a red-tier tool: switch it back on from Vex itself")
            from safety.confirm import ConfirmRequest

            req = ConfirmRequest(tool=t.name, tier="red", title=f"Switch the {t.name} tool back on?",
                                 detail=f"A local API client asked to switch on {t.name} (red tier: {t.description})",
                                 requires_non_voice=True, timeout=60.0)
            answer = self.dialog_confirmer.confirm(req)
            if not (answer.approved and answer.channel != "voice"):
                self._add_activity("tools", action="enable_refused", tool=t.name, tier=t.tier)
                raise ApiError(f"not switched on: {answer.reason or 'declined on screen'}")
        registry.set_enabled(t.name, True)
        log.info("tool %s switched back on (via the local API%s)", t.name, ", confirmed on screen" if t.tier == "red" else "")
        self._add_activity("tools", action="enable", tool=t.name, tier=t.tier)
        return {"name": t.name, "enabled": True}

    # ---- health, latency, autostart, budget -----------------------------------------------------------------------------------------
    def health(self, args: dict) -> dict:
        """Run the free health check now (a few seconds: it records 1 s of the microphone). The result is kept for health.last."""
        if self.health_fn is None:
            raise ApiError("the health check isn't available")
        r = dict(self.health_fn())
        r["at"] = _now()
        self.last_health = r
        return r

    def health_last(self, args: dict) -> dict | None:
        """The last health check result (None if none ran yet this session): cheap, for a dashboard tile."""
        return self.last_health

    # ---- conversations --------------------------------------------------------------------------------------------------------------
    def conversations_list(self, args: dict) -> list[dict]:
        if self.conversations is None:
            return []
        return self.conversations.list(max(1, min(200, int(args.get("limit", 50)))))

    def conversations_get(self, args: dict) -> dict:
        s = self.conversations.get(str(args.get("id", ""))) if self.conversations is not None else None
        if s is None:
            raise ApiError("no such conversation")
        return s

    # ---- reminders & tasks ------------------------------------------------------------------------------------------------------------
    def reminders_list(self, args: dict) -> list[dict]:
        from tools import reminders

        out = []
        for n, i in enumerate(reminders.pending(), 1):
            rule = i.get("repeat")
            out.append({"number": n, "id": i.get("id"), "text": i.get("text", ""), "due": i.get("due"),
                        "repeat": reminders.describe_rule(rule) if rule else None})
        return out

    def tasks_list(self, args: dict) -> list[dict]:
        from tools import tasks

        try:
            vault, path = tasks._open()                    # noqa: SLF001 - the task list's own reader
            body = vault.read(path).body
        except Exception:  # noqa: BLE001
            return []
        out = []
        for n, (_, done, text) in enumerate(tasks._tasks(body), 1):     # noqa: SLF001 - numbered like list_tasks / complete_task
            t, due = tasks.split_due(text)
            out.append({"number": n, "text": t, "due": due or None, "done": done})
        return out

    def _run_tool(self, name: str, args: dict) -> dict:
        """Through Vex's executor: the same safety tiers, confirmations and audit as when you ask by voice."""
        if self.executor is None:
            raise ApiError("not available")
        r = self.executor.run(name, args)
        self._add_activity("command", via="api", text=f"{name} {json.dumps(args, ensure_ascii=False)}"[:200])
        return {"ok": bool(r.ok), "message": r.message}

    def tasks_add(self, args: dict) -> dict:
        text = " ".join(str(args.get("text", "")).split())
        if not text:
            raise ApiError("text is empty")
        return self._run_tool("add_task", {"text": text[:300], "due": str(args.get("due", ""))[:80]})

    def tasks_complete(self, args: dict) -> dict:
        return self._run_tool("complete_task", {"task": str(int(args.get("number", 0)))})

    def tasks_delete(self, args: dict) -> dict:
        return self._run_tool("delete_task", {"task": str(int(args.get("number", 0)))})

    def reminders_add(self, args: dict) -> dict:
        text, when = " ".join(str(args.get("text", "")).split()), " ".join(str(args.get("when", "")).split())
        if not text or not when:
            raise ApiError("both text and when are needed (e.g. 'call mom', 'today at 7pm')")
        return self._run_tool("set_reminder", {"text": text[:300], "when": when[:120]})

    def reminders_cancel(self, args: dict) -> dict:
        return self._run_tool("cancel_reminder", {"which": str(int(args.get("number", 0)))})

    def latency(self, args: dict) -> dict:
        tools_dev = Path(__file__).resolve().parent.parent / "tools_dev"
        if str(tools_dev) not in sys.path:
            sys.path.insert(0, str(tools_dev))
        import latency_report

        rows = latency_report.load(self.latency_path)
        n = max(1, min(500, int(args.get("last", 30))))
        return latency_report.summary(rows[-n:])

    def latency_turns(self, args: dict) -> list[dict]:
        """The last spoken turns' timings, oldest first (the Voice screen's chart): ts, source, total and the stages."""
        tools_dev = Path(__file__).resolve().parent.parent / "tools_dev"
        if str(tools_dev) not in sys.path:
            sys.path.insert(0, str(tools_dev))
        import latency_report

        n = max(1, min(200, int(args.get("last", 40))))
        rows = [r for r in latency_report.load(self.latency_path) if r.get("outcome") == "spoken" and "total" in r.get("stages", {})]
        out = []
        for r in rows[-n:]:
            st = r["stages"]
            out.append({"ts": r.get("ts"), "source": r.get("source"), "total": st.get("total"), "stt": st.get("stt"),
                        "brain": st.get("brain"), "first_audio": st.get("tts_first_audio"), "followup": bool(r.get("followup"))})
        return out

    # ---- system & logs --------------------------------------------------------------------------------------------------------------------
    LOG_FILES = {"vex": "vex.log", "supervisor": "supervisor.log", "housekeeping": "housekeeping.log", "errors": "child_stderr.txt",
                 "health": "health.txt"}

    def system_info(self, args: dict) -> dict:
        import os
        import platform
        import subprocess as sp

        from config import ROOT

        try:
            commit = sp.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, sp.SubprocessError):
            commit = ""
        supervised = False
        try:
            import psutil

            proc = psutil.Process(os.getpid())
            for _ in range(3):                               # the venv's pythonw is a launcher: the supervisor can be a grandparent
                proc = proc.parent()
                if proc is None:
                    break
                if "--supervise" in " ".join(proc.cmdline()):
                    supervised = True
                    break
        except Exception:  # noqa: BLE001
            pass
        return {"pid": os.getpid(), "python": platform.python_version(), "windows": platform.platform(terse=True), "commit": commit,
                "supervised": supervised, "folder": str(ROOT), "uptime_s": int(self._clock() - self.started_at)}

    def system_restart(self, args: dict) -> dict:
        """Restart Vex: under the supervisor it comes straight back; otherwise the Control Center starts it again."""
        if self.restart_fn is None:
            raise ApiError("restart isn't available")
        log.info("restart requested from the local API")
        self._add_activity("system", action="restart")
        import threading as _t

        _t.Timer(0.4, self.restart_fn).start()                  # after this reply has gone out
        return {"restarting": True}

    def logs_list(self, args: dict) -> list[dict]:
        out = []
        for key, name in self.LOG_FILES.items():
            p = self.logs / name
            if p.exists():
                st = p.stat()
                out.append({"name": key, "file": name, "bytes": st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
        return out

    def logs_tail(self, args: dict) -> dict:
        """The last lines of one of Vex's own logs (keys and secrets hidden)."""
        name = self.LOG_FILES.get(str(args.get("name", "vex")))
        if name is None:
            raise ApiError("unknown log")
        n = max(10, min(2000, int(args.get("lines", 300))))
        p = self.logs / name
        try:
            with p.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return {"name": args.get("name"), "lines": []}
        from config import ROOT
        from ui import health

        redact = health.Redactor(health.secrets_from_env(ROOT))
        lines = [redact(x) for x in text.splitlines()[-n:]]
        return {"name": args.get("name"), "lines": lines}

    def logs_open(self, args: dict) -> dict:
        import os

        try:
            os.startfile(self.logs)  # noqa: S606
        except OSError as e:
            raise ApiError(f"couldn't open it: {e}") from e
        return {"opened": str(self.logs)}

    # ---- voice ---------------------------------------------------------------------------------------------------------------------------
    def _voice(self):
        if self.voice is None:
            raise ApiError("voice settings aren't available")
        return self.voice

    def voice_get(self, args: dict) -> dict:
        return self._voice().get()

    def voice_levels(self, args: dict) -> dict:
        return self._voice().levels()

    def voice_set(self, args: dict) -> dict:
        try:
            r = self._voice().set(args)
        except (ValueError, TypeError) as e:
            raise ApiError(str(e)) from e
        self._add_activity("voice", change=", ".join(f"{k} {v}" for k, v in args.items()))
        return r

    def voice_preview(self, args: dict) -> dict:
        try:
            return self._voice().preview(str(args.get("voice", "")))
        except ValueError as e:
            raise ApiError(str(e)) from e

    def autostart_get(self, args: dict) -> dict:
        from ui import autostart

        return {"on": autostart.is_enabled(), "choice": autostart.read_state()}

    def autostart_set(self, args: dict) -> dict:
        from ui import autostart

        if "on" not in args:
            raise ApiError("'on' (true/false) is required")
        if bool(args["on"]):
            autostart.enable()
        elif not autostart.disable():
            raise ApiError("couldn't remove the startup entry")
        self._add_activity("autostart", on=bool(args["on"]))
        return self.autostart_get({})

    def budget_models(self, args: dict) -> dict:
        """Per model: today's requests and tokens, tokens per day for the last week, and the free daily limit where one is known
        (Groq: 200K tokens per model per day). Every model Vex uses is a free tier: spend is 0."""
        from brain import usage as llm_usage
        from config import settings

        rec = llm_usage._recorder                      # noqa: SLF001 - the app's one usage store
        if rec is None:
            from brain.usage import LlmUsage

            rec = LlmUsage(self.data / "llm_usage.json")
        s = rec.summary(max(1, min(30, int(args.get("days", 7)))))
        limit = int(self.budget_cfg.get("groq_daily_tokens", 200_000))
        order = list(settings.get("llm", {}).get("order", []))
        for m in s["models"]:
            m["daily_limit"] = limit if m["provider"].startswith("groq") else None
            m["spend_inr"] = 0.0
        s["models"].sort(key=lambda m: (order.index(m["provider"]) if m["provider"] in order else 99, m["provider"]))
        return s

    def budget(self, args: dict) -> dict:
        """Money: everything Vex uses is free except Google Cloud TTS (off by default). Characters past the free allowance x price."""
        from config import settings
        from voice.usage import UsageStore

        cfg = self.budget_cfg
        budget_inr = float(cfg.get("budget_inr", 1000))
        usage = UsageStore(self.data / "voice_usage.json")
        chars = int(usage.month_total("google_chars"))
        free = int(settings["voice"].get("tts_monthly_free_chars", 1_000_000))
        billable = max(0, chars - free)
        used_inr = billable / 1e6 * float(cfg.get("google_tts_usd_per_million_chars", 30.0)) * float(cfg.get("usd_to_inr", 88.0))
        on = "google" in settings["voice"].get("tts_order", [])
        return {"budget_inr": budget_inr, "used_inr": round(used_inr, 2), "left_inr": round(budget_inr - used_inr, 2),
                "google_tts_on": on, "google_chars_this_month": chars, "google_free_chars": free,
                "note": ("Google Cloud TTS is in use: only characters past the free allowance cost money." if on else
                         "Google Cloud TTS is off: speech is edge-tts / the Windows voice, and the brains are free tiers. Nothing costs money.")}
