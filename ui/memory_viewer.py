"""The VexMind memory viewer: a normal window around ui/memory.html (graph, notes, backlinks, edit, inbox).

The page talks to Python only through a QWebChannel bridge, with no network port. The bridge forwards a whitelisted set of calls to
`memory.viewer_service.ViewerService` and returns JSON; a timer tells the page when notes changed on disk so it refreshes itself.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from memory.viewer_service import ViewerService

log = logging.getLogger("vex.memory.viewer")
PAGE = Path(__file__).resolve().parent / "memory.html"
ALLOWED = {"version", "graph", "sidebar", "notes_with_tag", "search", "resolve", "note", "raw", "save", "inbox", "approve", "discard"}


class Bridge(QObject):
    changed = Signal()

    def __init__(self, service: ViewerService):
        super().__init__()
        self.service = service
        self.ready_calls = 0

    @Slot(str, str, result=str)
    def call(self, method: str, args_json: str) -> str:
        """JSON in, JSON out. Unknown methods and errors become {"ok": false, "error": ...}, never an exception into the page."""
        if method not in ALLOWED:
            return json.dumps({"ok": False, "error": f"unknown call {method}"})
        try:
            args = json.loads(args_json or "[]")
            return json.dumps(getattr(self.service, method)(*args), ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001
            log.exception("viewer call %s failed", method)
            return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})

    @Slot()
    def ready(self) -> None:
        self.ready_calls += 1


class MemoryViewerWindow(QMainWindow):
    def __init__(self, service: ViewerService, parent=None):
        super().__init__(parent)
        self.setWindowTitle("VexMind")
        self.resize(1280, 800)
        self.service = service
        self.view = QWebEngineView(self)
        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        self.bridge = Bridge(service)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.setCentralWidget(self.view)
        self.view.load(QUrl.fromLocalFile(str(PAGE)))
        self._last = None
        self.timer = QTimer(self)                         # tell the page when notes changed (hand edits, curator, consolidation)
        self.timer.timeout.connect(self._poll)
        self.timer.start(1500)

    def _poll(self) -> None:
        if not self.isVisible():
            return
        try:
            v = self.service.version()
        except Exception:  # noqa: BLE001
            log.debug("viewer poll failed", exc_info=True)
            return
        if v != self._last:
            self._last = v
            self.bridge.changed.emit()

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
