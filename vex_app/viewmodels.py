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
