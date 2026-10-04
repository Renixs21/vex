"""View models: Python objects the QML binds to. They turn API replies and events into simple properties and offer slots for actions.

Every action goes through the API; nothing here can approve a confirmation (the API has no such command).
"""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from vex_app.client import ApiClient

log = logging.getLogger("vex_app")

TIER_ORDER = {"green": 0, "yellow": 1, "red": 2}


def _prop(name: str, kind: str, signal_name: str):
    """A read-only Qt property backed by self._<name>."""
    def getter(self):
        return getattr(self, "_" + name)

    return Property(kind, getter, notify=signal_name)


class StatusModel(QObject):
    """Vex's live state: drives the orb, the header chips, the confirm banner and the sidebar uptime."""

    changed = Signal()
    connectedChanged = Signal()
    toast = Signal(str)

    def __init__(self, client: ApiClient, start_vex=None, clock=time.monotonic, parent=None):
        super().__init__(parent)
        self.client = client
        self._clock = clock
        self._start_vex = start_vex or (lambda: None)
        self._status: dict = {}
        self._uptime_base = 0
        self._uptime_at = clock()
        client.event.connect(self._on_event)
        client.connectedChanged.connect(self._on_connected)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.changed)               # the uptime ticks without asking Vex
        self._tick.start(30_000)

    def _on_connected(self) -> None:
        self.connectedChanged.emit()
        if self.client.connected:
            self.refresh()

    @Slot()
    def refresh(self) -> None:
        self.client.call("state", {}, lambda ok, data, err: ok and self._apply(data))

    def _on_event(self, name: str, data: Any) -> None:
        if name == "state" and isinstance(data, dict):
            self._apply(data)
        elif name == "confirm" and isinstance(data, dict):
            self._status = {**self._status, "confirm": data}
            self.changed.emit()
        elif name == "confirm_done":
            self._status = {**self._status, "confirm": None}
            self.changed.emit()
        elif name == "tool" and isinstance(data, dict):
            self._status = {**self._status, "current_tool": data if data.get("running") else None}
            self.changed.emit()

    def _apply(self, data: dict) -> None:
        self._status = dict(data)
        if "uptime_s" in data:
            self._uptime_base, self._uptime_at = int(data["uptime_s"]), self._clock()
        self.changed.emit()

    # ---- properties -------------------------------------------------------------------------------------------------------------
    def _connected(self) -> bool:
        return self.client.connected

    connected = Property(bool, _connected, notify=connectedChanged)

    def _get(self, key, default=None):
        return self._status.get(key, default)

    state = Property(str, lambda self: str(self._get("state", "idle")), notify=changed)
    muted = Property(bool, lambda self: bool(self._get("muted", False)), notify=changed)
    asleep = Property(bool, lambda self: bool(self._get("asleep", False)), notify=changed)
    followupEnabled = Property(bool, lambda self: bool(self._get("followup_enabled", False)), notify=changed)
    followupSeconds = Property(int, lambda self: int(round(float(self._get("followup_seconds", 0) or 0))), notify=changed)
    wakeword = Property(str, lambda self: str(self._get("wakeword", "off")), notify=changed)
    hotkey = Property(str, lambda self: str(self._get("hotkey", "") or "Ctrl+Alt+V"), notify=changed)
    confirm = Property("QVariant", lambda self: self._get("confirm"), notify=changed)
    currentTool = Property("QVariant", lambda self: self._get("current_tool"), notify=changed)

    def _uptime_text(self) -> str:
        s = self._uptime_base + int(self._clock() - self._uptime_at) if self._status else 0
        h, m = divmod(s // 60, 60)
        d, h = divmod(h, 24)
        return f"{d}d {h}h" if d else (f"{h}h {m:02d}m" if h else f"{m}m")

    uptime = Property(str, _uptime_text, notify=changed)

    def _listening_text(self) -> str:
        if not self.client.connected:
            return "Not connected"
        if self._get("muted"):
            return "Microphone muted"
        st = self._get("state", "idle")
        if st == "followup":
            return "Still listening"
        if st in ("listening", "thinking", "speaking"):
            return st.capitalize()
        if self._get("wakeword") == "running":
            return "Listening for ‘Hey Vex’"
        return f"Hotkey only ({self.hotkey})"

    headline = Property(str, _listening_text, notify=changed)

    # ---- actions -------------------------------------------------------------------------------------------------------------
    @Slot(bool)
    def setMuted(self, on: bool) -> None:
        self.client.call("mute", {"on": bool(on)}, lambda ok, d, e: ok and self.refresh())

    @Slot()
    def listen(self) -> None:
        self.client.call("listen", {})

    @Slot(str)
    def send(self, text: str) -> None:
        text = " ".join((text or "").split())
        if text:
            self.client.call("text", {"text": text}, lambda ok, d, e: (not ok) and self.toast.emit(e or "Couldn't send that"))

    @Slot()
    def startVex(self) -> None:
        self._start_vex()
        self.toast.emit("Starting Vex…")


class HomeModel(QObject):
    """The Home screen's side data: today's reminders and tasks, recent activity, response time, health, budget."""

    changed = Signal()
    MAX_ACTIVITY = 10

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._today: list = []
        self._activity: list = []
        self._latency: dict = {}
        self._health: dict = {}
        self._budget: dict = {}
        self._healthBusy = False
        client.connectedChanged.connect(lambda: client.connected and self.refresh())
        client.event.connect(self._on_event)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)

    @Slot()
    def refresh(self) -> None:
        c = self.client
        if not c.connected:
            return
        c.call("reminders.list", {}, self._got_reminders)
        c.call("activity", {"limit": self.MAX_ACTIVITY}, self._got_activity)
        c.call("latency", {"last": 30}, lambda ok, d, e: ok and self._set("_latency", d or {}))
        c.call("health.last", {}, lambda ok, d, e: ok and d and self._set("_health", d))
        c.call("budget", {}, lambda ok, d, e: ok and self._set("_budget", d or {}))

    def _set(self, attr: str, value) -> None:
        setattr(self, attr, value)
        self.changed.emit()

    def _got_reminders(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        today = datetime.now().date()
        items = []
        for r in data or []:
            try:
                due = datetime.fromisoformat(r["due"])
            except (KeyError, TypeError, ValueError):
                continue
            if due.date() <= today:
                items.append({"time": due.strftime("%H:%M"), "text": r.get("text", ""), "repeat": r.get("repeat") or "",
                              "overdue": due < datetime.now(due.tzinfo)})
        self._set("_today", items)

    def _got_activity(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        items = [self.describe(a) for a in (data or [])]
        if len(items) < self.MAX_ACTIVITY:                          # a fresh Vex: fill in from the audit log
            self.client.call("audit", {"limit": self.MAX_ACTIVITY}, lambda ok2, rows, e: self._merge_audit(items, rows if ok2 else []))
        else:
            self._set("_activity", list(reversed(items))[: self.MAX_ACTIVITY])

    def _merge_audit(self, items: list, rows) -> None:
        audit = [self.describe({"ts": r.get("ts", ""), "kind": "tool", "tool": r.get("tool"), "tier": r.get("tier"),
                                "decision": r.get("decision"), "ok": r.get("ok"), "summary": r.get("result") or r.get("summary") or ""}) for r in rows or []]
        seen = {(a["time"], a["title"]) for a in items}
        merged = items + [a for a in audit if (a["time"], a["title"]) not in seen]
        merged.sort(key=lambda a: a.get("ts", ""), reverse=True)
        self._set("_activity", merged[: self.MAX_ACTIVITY])

    def _on_event(self, name: str, data) -> None:
        if name == "activity" and isinstance(data, dict):
            self._activity = ([self.describe(data)] + self._activity)[: self.MAX_ACTIVITY]
            self.changed.emit()

    @staticmethod
    def describe(a: dict) -> dict:
        """One activity row: time (mono), title (a tool name is mono), detail, tier colour dot."""
        ts = str(a.get("ts", ""))
        try:
            t = datetime.fromisoformat(ts).strftime("%H:%M")
        except ValueError:
            t = ts[11:16]
        kind = a.get("kind")
        if kind == "tool":
            verb = {"denied": "declined", "timeout": "no answer", "blocked": "blocked", "disabled": "switched off",
                    "dry_run": "dry run"}.get(str(a.get("decision")), "")
            detail = (str(a.get("summary") or "") or verb)[:120]
            return {"ts": ts, "time": t, "title": str(a.get("tool") or "?"), "mono": True, "tier": a.get("tier") or "",
                    "detail": detail + (f" ({verb})" if verb and detail != verb else ""), "ok": bool(a.get("ok"))}
        if kind == "turn":
            return {"ts": ts, "time": t, "title": "You", "mono": False, "tier": "", "detail": str(a.get("said", ""))[:120], "ok": True}
        if kind == "confirm":
            return {"ts": ts, "time": t, "title": str(a.get("tool") or "confirm"), "mono": True, "tier": a.get("tier") or "",
                    "detail": "approved" if a.get("approved") else "declined", "ok": bool(a.get("approved"))}
        if kind == "command":
            return {"ts": ts, "time": t, "title": "You (typed)", "mono": False, "tier": "", "detail": str(a.get("text", ""))[:120], "ok": True}
        title = {"memory": "memory", "tools": "tools", "autostart": "autostart"}.get(str(kind), str(kind))
        detail = " ".join(f"{k} {v}" for k, v in a.items() if k not in ("ts", "kind"))[:120]
        return {"ts": ts, "time": t, "title": title, "mono": True, "tier": a.get("tier") or "", "detail": detail, "ok": True}

    @Slot()
    def runHealth(self) -> None:
        if self._healthBusy:
            return
        self._healthBusy = True
        self.changed.emit()

        def done(ok, d, e):
            self._healthBusy = False
            if ok and d:
                self._health = d
            self.changed.emit()

        self.client.call("health", {}, done)

    today = Property("QVariantList", lambda self: self._today, notify=changed)
    activity = Property("QVariantList", lambda self: self._activity, notify=changed)
    healthBusy = Property(bool, lambda self: self._healthBusy, notify=changed)

    def _latency_view(self) -> dict:
        st = (self._latency or {}).get("stages", {})
        tot, first = st.get("total", {}), st.get("tts_first_audio", {})
        return {"median": tot.get("median"), "firstAudio": first.get("median"), "turns": (self._latency or {}).get("spoken", 0)}

    latency = Property("QVariantMap", _latency_view, notify=changed)

    def _health_view(self) -> dict:
        h = self._health or {}
        checks = h.get("checks") or []
        fails = sum(1 for c in checks if c.get("status") == "FAIL")
        warns = sum(1 for c in checks if c.get("status") == "WARN")
        return {"summary": h.get("summary") or "Not checked yet this session", "at": str(h.get("at") or "")[11:16],
                "fails": fails, "warns": warns, "ran": bool(h)}

    health = Property("QVariantMap", _health_view, notify=changed)

    def _budget_view(self) -> dict:
        b = self._budget or {}
        total = float(b.get("budget_inr") or 1000)
        used = float(b.get("used_inr") or 0)
        return {"used": used, "total": total, "fraction": max(0.0, min(1.0, used / total if total else 0)), "note": b.get("note", "")}

    budget = Property("QVariantMap", _budget_view, notify=changed)


def start_vex_process(command: list[str]) -> None:
    """Start Vex detached (the "Start it" button). The Control Center never imports Vex; it only launches it."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(command, cwd=command[1].rsplit("main.py", 1)[0] or None, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        log.exception("could not start Vex")


TYPE_LABELS = {"project": "Project", "person": "Person", "preference": "Preference", "profile": "Profile", "topic": "Topic",
               "place": "Place", "daily": "Daily log", "task-list": "Tasks", "ghost": "Not written yet"}
TYPE_COLORS = {"project": "#93a8bf", "person": "#cdb98a", "preference": "#c8896a"}
FACT_COLOR = "#b9bec6"


class MemoryModel(QObject):
    """The Memory screen: Focus (a note + 2 hops), Galaxy (everything), Timeline, Notes, the selected note and the inbox."""

    changed = Signal()
    focusGraphChanged = Signal()
    galaxyChanged = Signal()
    noteChanged = Signal()
    toast = Signal(str)
    forgetPreview = Signal(str)                # the app shows this in its own "are you sure?" dialog
    rawLoaded = Signal(str)                    # the editor opens with this text

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._focusId = ""
        self._focusGraph: dict = {}
        self._galaxy: dict = {}
        self._timeline: list = []
        self._notes: list = []
        self._note: dict = {}
        self._inbox: list = []
        self._size: dict = {}
        self._results: list = []
        client.connectedChanged.connect(lambda: client.connected and self.refresh())

    # ---- loading ------------------------------------------------------------------------------------------------------------------
    @Slot()
    def refresh(self) -> None:
        if not self.client.connected:
            return
        self.client.call("memory.list", {}, self._got_notes)
        self.client.call("memory.inbox", {}, lambda ok, d, e: ok and self._set("_inbox", [self._inbox_view(i) for i in d or []]))
        self.client.call("memory.size", {}, lambda ok, d, e: ok and self._set("_size", d or {}))

    def _set(self, attr: str, value) -> None:
        setattr(self, attr, value)
        self.changed.emit()

    def _got_notes(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        self._notes = [{**n, "typeLabel": TYPE_LABELS.get(n["type"], n["type"].capitalize()), "color": self.color(n["type"]),
                        "updatedText": str(n.get("updated", ""))[:10]} for n in data or []]
        self.changed.emit()
        if not self._focusId and self._notes:                       # start on the best-connected note, so Focus shows a neighbourhood
            self.client.call("memory.graph", {"daily": True}, self._pick_start)

    def _pick_start(self, ok: bool, data, err: str) -> None:
        if self._focusId or not self._notes:
            return
        links = {n["id"]: n.get("links", 0) for n in ((data or {}).get("nodes") or []) if not n.get("ghost")} if ok else {}
        best = max(self._notes, key=lambda n: (links.get(n["id"], 0), n["type"] != "daily", n["type"] == "profile"))
        self.select(best["id"])

    @staticmethod
    def color(type_: str) -> str:
        return TYPE_COLORS.get(type_, FACT_COLOR)

    @staticmethod
    def _inbox_view(i: dict) -> dict:
        bits = [str(i.get("note") or ""), str(i.get("type") or "")]
        if i.get("confidence") is not None:
            bits.append(f"confidence {float(i['confidence']):.2f}")
        return {**i, "text": i.get("fact") or i.get("text") or "", "detail": " · ".join(b for b in bits if b)}

    @Slot(str)
    def select(self, note_id: str) -> None:
        if not note_id or note_id.startswith("ghost:"):
            return
        self._focusId = note_id
        self.client.call("memory.neighbours", {"id": note_id, "hops": 2}, self._got_focus)
        self.client.call("memory.note", {"id": note_id}, self._got_note)

    def _got_focus(self, ok: bool, data, err: str) -> None:
        if ok:
            self._focusGraph = data or {}
            self.focusGraphChanged.emit()

    def _got_note(self, ok: bool, data, err: str) -> None:
        if not ok:
            self.toast.emit(err or "Couldn't open that note")
            return
        n = data or {}
        facts = [{"text": self._unlink(f.get("text", "")), "learned": str(f.get("learned") or "")[:10], "source": f.get("source") or ""}
                 for f in n.get("facts", [])]
        t = n.get("type", "")
        self._note = {"id": n.get("id"), "title": n.get("title", ""), "type": t, "path": n.get("path", ""),
                      "typeLabel": TYPE_LABELS.get(t, str(t).capitalize()), "color": self.color(t),
                      "body": self._plain_body(n.get("body", "")), "facts": facts,
                      "links": [{"id": x["id"], "title": x["title"]} for x in n.get("links_out", [])],
                      "backlinks": [{"id": b["id"], "title": b["title"]} for b in n.get("backlinks", [])][:12],
                      "created": str(n.get("created") or "")[:10], "updated": str(n.get("updated") or "")[:10]}
        self.noteChanged.emit()

    @staticmethod
    def _unlink(text: str) -> str:
        """[[Aquarium]] -> Aquarium, [[Rahul|my roommate]] -> my roommate (the links are shown as chips)."""
        import re

        return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), text or "")

    @staticmethod
    def _plain_body(body: str) -> str:
        """The note's free text without the fact lines (shown separately), headings and block ids."""
        import re

        body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)        # the vault's own metadata comments
        lines = [x for x in body.splitlines() if not re.match(r"^\s*-\s", x) and not x.startswith("#")]
        text = "\n".join(x.rstrip() for x in lines).strip()
        return re.sub(r"\s*\^[\w-]+$", "", text, flags=re.M)

    @Slot()
    def loadGalaxy(self) -> None:
        def done(ok, d, e):
            if ok:
                self._galaxy = d or {}
                self.galaxyChanged.emit()

        self.client.call("memory.graph", {"daily": True}, done)          # the full graph: daily logs hold many of the links

    @Slot()
    def loadTimeline(self) -> None:
        self.client.call("memory.timeline", {"limit": 300}, self._got_timeline)

    def _got_timeline(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        days = []
        for d in data or []:
            try:
                label = datetime.fromisoformat(d["day"]).strftime("%a %d %b %Y")
            except ValueError:
                label = d["day"]
            days.append({"day": d["day"], "label": label,
                         "items": [{**i, "color": self.color(i.get("type", "")), "typeLabel": TYPE_LABELS.get(i.get("type", ""), "")}
                                   for i in d.get("items", [])]})
        self._set("_timeline", days)

    @Slot(str)
    def search(self, query: str) -> None:
        q = (query or "").strip()
        if not q:
            self._set("_results", [])
            return
        self.client.call("memory.search", {"query": q, "limit": 12},
                         lambda ok, d, e: ok and self._set("_results", [{**r, "color": self.color(r.get("type", ""))} for r in d or []]))

    # ---- actions -------------------------------------------------------------------------------------------------------------------
    @Slot()
    def openNote(self) -> None:
        if self._note.get("id"):
            self.client.call("memory.open", {"id": self._note["id"]}, lambda ok, d, e: (not ok) and self.toast.emit(e))

    @Slot()
    def editNote(self) -> None:
        if self._note.get("id"):
            self.client.call("memory.raw", {"id": self._note["id"]},
                             lambda ok, d, e: ok and self.rawLoaded.emit((d or {}).get("text", "")))

    @Slot(str)
    def saveNote(self, text: str) -> None:
        nid = self._note.get("id")
        if not nid:
            return

        def done(ok, d, e):
            self.toast.emit("Saved." if ok else (e or "Not saved"))
            if ok:
                self.select(nid)
                self.refresh()

        self.client.call("memory.save", {"id": nid, "text": text}, done)

    @Slot()
    def askForget(self) -> None:
        """Step 1: ask Vex what forgetting this note would do; the app shows it and asks you."""
        if self._note.get("id"):
            self.client.call("memory.forget", {"id": self._note["id"]},
                             lambda ok, d, e: self.forgetPreview.emit((d or {}).get("message", "") if ok else (e or "")))

    @Slot()
    def confirmForget(self) -> None:
        """Step 2, only after you said yes in the dialog: the note goes to the Recycle Bin."""
        nid = self._note.get("id")
        if not nid:
            return

        def done(ok, d, e):
            self.toast.emit((d or {}).get("message", "Done.") if ok else (e or "Not forgotten"))
            self._note, self._focusId = {}, ""
            self.noteChanged.emit()
            self.refresh()

        self.client.call("memory.forget", {"id": nid, "confirm": True}, done)

    @Slot(int)
    def keep(self, index: int) -> None:
        self._inbox_action("memory.keep", index)

    @Slot(int)
    def discard(self, index: int) -> None:
        self._inbox_action("memory.discard", index)

    def _inbox_action(self, cmd: str, index: int) -> None:
        if not 0 <= index < len(self._inbox):
            return
        item = self._inbox[index]

        def done(ok, d, e):
            self.toast.emit(("Kept." if cmd.endswith("keep") else "Discarded.") if ok else (e or "That didn't work"))
            self.refresh()

        self.client.call(cmd, {"path": item.get("path"), "block": item.get("block")}, done)

    # ---- properties ----------------------------------------------------------------------------------------------------------------
    focusId = Property(str, lambda self: self._focusId, notify=focusGraphChanged)
    focusGraph = Property("QVariantMap", lambda self: self._focusGraph, notify=focusGraphChanged)
    galaxy = Property("QVariantMap", lambda self: self._galaxy, notify=galaxyChanged)
    timeline = Property("QVariantList", lambda self: self._timeline, notify=changed)
    notes = Property("QVariantList", lambda self: self._notes, notify=changed)
    note = Property("QVariantMap", lambda self: self._note, notify=noteChanged)
    inbox = Property("QVariantList", lambda self: self._inbox, notify=changed)
    results = Property("QVariantList", lambda self: self._results, notify=changed)

    def _size_view(self) -> dict:
        s = self._size or {}
        if not s:
            return {"mb": 0, "cap": 200, "fraction": 0.0, "text": ""}
        return {"mb": s.get("mb", 0), "cap": s.get("cap_mb", 200), "fraction": min(1.0, float(s.get("percent", 0) or 0) / 100),
                "text": f"{float(s.get('mb', 0)):.1f} MB of {float(s.get('cap_mb', 200)):.0f} MB"}

    size = Property("QVariantMap", _size_view, notify=changed)


class ToolsModel(QObject):
    """Tools & permissions: every tool with its tier, on/off and last use; the audit log with Undo/Restore for the last undoable action."""

    changed = Signal()
    toast = Signal(str)

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._tools: list = []
        self._audit: list = []
        self._undo: dict | None = None
        self._tier = "all"
        self._query = ""
        self._pending: set[str] = set()                         # red tools waiting for your answer on Vex's screen
        client.connectedChanged.connect(lambda: client.connected and self.refresh())
        client.event.connect(self._on_event)

    @Slot()
    def refresh(self) -> None:
        if not self.client.connected:
            return
        self.client.call("tools.list", {}, self._got_tools)
        self.client.call("audit", {"limit": 60}, self._got_audit)
        self.client.call("undo.peek", {}, lambda ok, d, e: ok and self._set("_undo", d))

    def _set(self, attr: str, value) -> None:
        setattr(self, attr, value)
        self.changed.emit()

    def _on_event(self, name: str, data) -> None:
        if name == "activity" and isinstance(data, dict) and data.get("kind") in ("tool", "tools"):
            self.refresh()

    def _got_tools(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        rows = []
        for t in data or []:
            rows.append({"name": t["name"], "tier": t["tier"], "enabled": bool(t["enabled"]), "description": t.get("description", ""),
                         "category": t.get("category", ""), "lastUsed": self._when(t.get("last_used"))})
        rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 9), r["name"]))
        self._set("_tools", rows)

    @staticmethod
    def _when(ts) -> str:
        if not ts:
            return "never"
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            return str(ts)[:16]
        now = datetime.now(dt.tzinfo)
        return dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%d %b %H:%M")

    @staticmethod
    def _short_when(ts) -> str:
        """Today: 14:05. Earlier: 03 Oct (the audit column is narrow)."""
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            return str(ts)[:5]
        return dt.strftime("%H:%M") if dt.date() == datetime.now(dt.tzinfo).date() else dt.strftime("%d %b")

    def _got_audit(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        rows = []
        for r in reversed(data or []):
            decision = str(r.get("decision", ""))
            rows.append({"id": r.get("id", ""), "time": self._short_when(r.get("ts")), "tool": r.get("tool", "?"), "tier": r.get("tier", ""),
                         "decision": {"auto": "ran", "approved": "approved", "denied": "declined", "timeout": "no answer",
                                      "blocked": "blocked", "refused": "refused", "dry_run": "dry run", "disabled": "switched off"}
                         .get(decision, decision),
                         "ok": bool(r.get("ok")), "result": str(r.get("result") or r.get("reason") or "")[:160],
                         "undoable": bool(r.get("undoable")), "via": self._via(r.get("via"))})
        self._set("_audit", rows)

    @staticmethod
    def _via(via) -> str:
        if isinstance(via, dict) and via:
            return ", ".join(sorted(set(str(v) for v in via.values())))
        return ""

    # ---- filters ----------------------------------------------------------------------------------------------------------------
    @Slot(str)
    def setTier(self, tier: str) -> None:
        self._set("_tier", tier or "all")

    @Slot(str)
    def setQuery(self, q: str) -> None:
        self._set("_query", (q or "").strip().lower())

    def _visible(self) -> list:
        out = []
        for t in self._tools:
            if self._tier != "all" and t["tier"] != self._tier:
                continue
            if self._query and self._query not in t["name"].lower() and self._query not in t["description"].lower():
                continue
            out.append({**t, "pending": t["name"] in self._pending})
        return out

    def _counts(self) -> dict:
        c = {"all": len(self._tools), "green": 0, "yellow": 0, "red": 0, "off": 0}
        for t in self._tools:
            c[t["tier"]] = c.get(t["tier"], 0) + 1
            c["off"] += 0 if t["enabled"] else 1
        return c

    # ---- actions ------------------------------------------------------------------------------------------------------------------
    @Slot(str, bool)
    def setEnabled(self, name: str, on: bool) -> None:
        tool = next((t for t in self._tools if t["name"] == name), None)
        if tool is None or tool["enabled"] == on or name in self._pending:
            return
        if on and tool["tier"] == "red":
            self._pending.add(name)
            self.changed.emit()
            self.toast.emit(f"Confirm on Vex's screen to switch {name} back on")

        def done(ok, d, e):
            self._pending.discard(name)
            if not ok:
                self.toast.emit(e or "That didn't work")
            self.refresh()

        self.client.call("tools.enable" if on else "tools.disable", {"name": name}, done)

    @Slot()
    def undo(self) -> None:
        def done(ok, d, e):
            self.toast.emit((d or {}).get("message") if ok and d else (e or "Couldn't undo that"))
            self.refresh()

        self.client.call("undo", {}, done)

    tools = Property("QVariantList", _visible, notify=changed)
    audit = Property("QVariantList", lambda self: self._audit, notify=changed)
    counts = Property("QVariantMap", _counts, notify=changed)
    tier = Property(str, lambda self: self._tier, notify=changed)

    def _undo_view(self) -> dict:
        u = self._undo or {}
        if not u:
            return {"available": False, "auditId": "", "label": "", "verb": ""}
        return {"available": True, "auditId": u.get("audit_id") or "", "label": u.get("label") or "",
                "verb": "Restore" if u.get("kind") == "deleted" else "Undo"}

    undoInfo = Property("QVariantMap", _undo_view, notify=changed)


def _clock_text(ts: str, with_date: bool = False) -> str:
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return str(ts)[:16]
    today = datetime.now(dt.tzinfo).date()
    if dt.date() == today and not with_date:
        return dt.strftime("%H:%M")
    if dt.date() == today:
        return "Today " + dt.strftime("%H:%M")
    if (today - dt.date()).days == 1:
        return "Yesterday " + dt.strftime("%H:%M")
    return dt.strftime("%a %d %b %H:%M")


class ConversationsModel(QObject):
    """Conversations: sessions (a wake-up and its follow-ups) with the transcript, tools used and per-turn latency."""

    changed = Signal()
    detailChanged = Signal()

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._sessions: list = []
        self._detail: dict = {}
        self._selected = ""
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(1500)
        self._debounce.timeout.connect(self.refresh)
        client.connectedChanged.connect(lambda: client.connected and self.refresh())
        client.event.connect(self._on_event)

    def _on_event(self, name: str, data) -> None:
        if name == "state" and isinstance(data, dict) and data.get("state") in ("idle", "asleep", "muted"):
            self._debounce.start()                              # a session may just have ended

    @Slot()
    def refresh(self) -> None:
        if self.client.connected:
            self.client.call("conversations.list", {"limit": 100}, self._got_list)

    def _got_list(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        rows = []
        for s in data or []:
            mt = s.get("median_total")
            rows.append({"id": s["id"], "when": _clock_text(s["start"], with_date=True), "first": s.get("first") or "",
                         "turns": int(s.get("turns", 0)), "tools": s.get("tools") or [],
                         "latency": "" if mt is None else f"{mt:.1f} s"})
        self._sessions = rows
        self.changed.emit()
        if rows and (not self._selected or not any(r["id"] == self._selected for r in rows)):
            self.select(rows[0]["id"])

    @Slot(str)
    def select(self, session_id: str) -> None:
        self._selected = session_id
        self.changed.emit()
        self.client.call("conversations.get", {"id": session_id}, self._got_detail)

    def _got_detail(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        turns = []
        for t in (data or {}).get("turns", []):
            lat = t.get("latency") or {}
            parts = [f"{k} {lat[key]:.1f} s" for k, key in (("total", "total"), ("stt", "stt"), ("brain", "brain"),
                                                             ("first sound", "tts_first_audio")) if key in lat]
            turns.append({"time": _clock_text(t.get("ts", "")), "said": t.get("said", ""), "reply": t.get("reply", ""),
                          "tools": [{"tool": x.get("tool"), "tier": x.get("tier") or "", "ok": bool(x.get("ok")),
                                     "decision": x.get("decision") or ""} for x in t.get("tools", [])],
                          "latency": " · ".join(parts), "via": t.get("via") or ""})
        self._detail = {"id": (data or {}).get("id"), "when": _clock_text((data or {}).get("start", ""), with_date=True), "turns": turns}
        self.detailChanged.emit()

    sessions = Property("QVariantList", lambda self: self._sessions, notify=changed)
    selected = Property(str, lambda self: self._selected, notify=changed)
    detail = Property("QVariantMap", lambda self: self._detail, notify=detailChanged)


class TasksModel(QObject):
    """Tasks & reminders: your to-do list (VexVault/tasks.md) and the reminders Vex will speak. Every change goes through Vex's own
    tools, so it is audited and asks first where it would by voice (deleting a task, cancelling all reminders)."""

    changed = Signal()
    toast = Signal(str)

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._tasks: list = []
        self._reminders: list = []
        client.connectedChanged.connect(lambda: client.connected and self.refresh())
        client.event.connect(lambda name, data: name == "activity" and isinstance(data, dict)
                             and str(data.get("tool", "")).endswith(("task", "reminder")) and self.refresh())

    @Slot()
    def refresh(self) -> None:
        if not self.client.connected:
            return
        self.client.call("tasks.list", {}, self._got_tasks)
        self.client.call("reminders.list", {}, self._got_reminders)

    def _got_tasks(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        rows = [{"number": t["number"], "text": t.get("text", ""), "due": self._due_text(t.get("due")), "done": bool(t.get("done")),
                 "overdue": self._overdue(t.get("due")) and not t.get("done")} for t in data or []]
        rows.sort(key=lambda r: (r["done"], r["number"]))
        self._tasks = rows
        self.changed.emit()

    def _got_reminders(self, ok: bool, data, err: str) -> None:
        if not ok:
            return
        self._reminders = [{"number": r["number"], "text": r.get("text", ""), "when": self._due_text(r.get("due"), True),
                            "repeat": r.get("repeat") or ""} for r in data or []]
        self.changed.emit()

    @staticmethod
    def _due_text(due, with_time: bool = False) -> str:
        if not due:
            return ""
        try:
            dt = datetime.fromisoformat(str(due))
        except ValueError:
            return str(due)
        today = datetime.now(dt.tzinfo).date() if dt.tzinfo else datetime.now().date()
        has_time = with_time or len(str(due)) > 10
        day = "Today" if dt.date() == today else ("Tomorrow" if (dt.date() - today).days == 1 else dt.strftime("%a %d %b"))
        return day + (dt.strftime(" %H:%M") if has_time else "")

    @staticmethod
    def _overdue(due) -> bool:
        if not due:
            return False
        try:
            dt = datetime.fromisoformat(str(due))
        except ValueError:
            return False
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return dt < now if len(str(due)) > 10 else dt.date() < now.date()

    def _done(self, ok, d, e) -> None:
        msg = (d or {}).get("message") if ok else e
        if msg:
            self.toast.emit(msg.split("\n")[0][:160])
        self.refresh()

    @Slot(str, str)
    def addTask(self, text: str, due: str) -> None:
        if text.strip():
            self.client.call("tasks.add", {"text": text, "due": due}, self._done)

    @Slot(int)
    def completeTask(self, number: int) -> None:
        self.client.call("tasks.complete", {"number": number}, self._done)

    @Slot(int)
    def deleteTask(self, number: int) -> None:
        self.toast.emit("Confirm on Vex's screen")
        self.client.call("tasks.delete", {"number": number}, self._done)

    @Slot(str, str)
    def addReminder(self, text: str, when: str) -> None:
        if text.strip() and when.strip():
            self.client.call("reminders.add", {"text": text, "when": when}, self._done)

    @Slot(int)
    def cancelReminder(self, number: int) -> None:
        self.client.call("reminders.cancel", {"number": number}, self._done)

    tasks = Property("QVariantList", lambda self: self._tasks, notify=changed)
    reminders = Property("QVariantList", lambda self: self._reminders, notify=changed)
    openCount = Property(int, lambda self: sum(1 for t in self._tasks if not t["done"]), notify=changed)


class VoiceModel(QObject):
    """Voice: wake word sensitivity with a live mic meter and wake score, the follow-up window, Vex's voice, and the response-time chart."""

    changed = Signal()
    levelsChanged = Signal()
    toast = Signal(str)

    def __init__(self, client: ApiClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._settings: dict = {}
        self._levels: dict = {"level": 0.0, "score": 0.0, "threshold": 0.5, "listening": False}
        self._turns: list = []
        self._poll = QTimer(self)
        self._poll.setInterval(120)                          # ~8 per second while the Voice screen is open
        self._poll.timeout.connect(self._ask_levels)
        self._waiting = False
        client.connectedChanged.connect(lambda: client.connected and self.refresh())

    @Slot()
    def refresh(self) -> None:
        if not self.client.connected:
            return
        self.client.call("voice.get", {}, lambda ok, d, e: ok and self._set("_settings", d or {}))
        self.client.call("latency.turns", {"last": 40}, lambda ok, d, e: ok and self._set("_turns", d or []))

    def _set(self, attr: str, value) -> None:
        setattr(self, attr, value)
        self.changed.emit()

    @Slot(bool)
    def setLive(self, on: bool) -> None:
        """Poll the mic level only while the screen is visible."""
        (self._poll.start if on else self._poll.stop)()

    def _ask_levels(self) -> None:
        if self._waiting or not self.client.connected:
            return
        self._waiting = True

        def done(ok, d, e):
            self._waiting = False
            if ok and d:
                self._levels = d
                self.levelsChanged.emit()

        self.client.call("voice.levels", {}, done)

    def _apply(self, args: dict, message: str = "") -> None:
        def done(ok, d, e):
            if ok:
                self._set("_settings", d or {})
                if message:
                    self.toast.emit(message)
            else:
                self.toast.emit(e or "Not changed")
                self.refresh()

        self.client.call("voice.set", args, done)

    @Slot(float)
    def setThreshold(self, value: float) -> None:
        self._apply({"threshold": round(float(value), 3)}, f"Wake word threshold {value:.2f}")

    @Slot(int)
    def setFollowupSeconds(self, seconds: int) -> None:
        self._apply({"followup_seconds": int(seconds)})

    @Slot(bool)
    def setFollowupEnabled(self, on: bool) -> None:
        self._apply({"followup_enabled": bool(on)})

    @Slot(str)
    def setVoice(self, name: str) -> None:
        self._apply({"voice": name}, "Vex's voice changed")

    @Slot(str)
    def preview(self, name: str) -> None:
        self.client.call("voice.preview", {"voice": name}, lambda ok, d, e: (not ok) and self.toast.emit(e or "Couldn't play that"))

    def _get(self, *keys, default=None):
        v = self._settings
        for k in keys:
            v = v.get(k, {}) if isinstance(v, dict) else {}
        return default if v in ({}, None) else v

    wakeAvailable = Property(bool, lambda self: bool(self._get("wake", "available", default=False)), notify=changed)
    wakeLabel = Property(str, lambda self: str(self._get("wake", "label", default="")), notify=changed)
    threshold = Property(float, lambda self: float(self._get("wake", "threshold", default=0.5)), notify=changed)
    followupEnabled = Property(bool, lambda self: bool(self._get("followup", "enabled", default=False)), notify=changed)
    followupSeconds = Property(int, lambda self: int(round(float(self._get("followup", "seconds", default=0)))), notify=changed)
    voice = Property(str, lambda self: str(self._get("voice", "current", default="")), notify=changed)

    def _voices(self) -> list:
        out = []
        for name in self._get("voice", "candidates", default=[]) or []:
            parts = name.split("-")
            region = "-".join(parts[:2]) if len(parts) >= 2 else ""
            nice = parts[2].replace("Neural", "") if len(parts) >= 3 else name
            nice = nice.replace("Expressive", " (expressive)").replace("Multilingual", " (multilingual)")
            out.append({"name": name, "label": nice, "region": region})
        return out

    voices = Property("QVariantList", _voices, notify=changed)
    turns = Property("QVariantList", lambda self: self._turns, notify=changed)
    level = Property(float, lambda self: float(self._levels.get("level", 0.0)), notify=levelsChanged)
    score = Property(float, lambda self: float(self._levels.get("score", 0.0)), notify=levelsChanged)
    listening = Property(bool, lambda self: bool(self._levels.get("listening", False)), notify=levelsChanged)
