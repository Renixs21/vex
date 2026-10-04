"""The Control Center's only link to Vex: a websocket client for the local API (docs/API.md).

  * Connects to ws://127.0.0.1:<port>/ws with the token from data/api_token (re-read on every attempt, so a new token just works).
  * While Vex isn't running it retries every few seconds; `connected` tells the UI which screen to show.
  * `call(cmd, args, callback)` sends a request; the callback gets (ok, data, error) on the Qt thread.
  * Pushed events arrive as the `event(name, data)` signal.
"""
from __future__ import annotations

import itertools
import json
import logging
from typing import Any, Callable

from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

log = logging.getLogger("vex_app.client")

Callback = Callable[[bool, Any, str], None]


class ApiClient(QObject):
    connectedChanged = Signal()
    event = Signal(str, "QVariant")

    def __init__(self, port: int, token_reader: Callable[[], str], retry_ms: int = 3000, parent: QObject | None = None):
        super().__init__(parent)
        self.port = port
        self._token_reader = token_reader
        self.retry_ms = retry_ms
        self._connected = False
        self._ids = itertools.count(1)
        self._pending: dict[int, Callback] = {}
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_open)
        self._ws.disconnected.connect(self._on_closed)
        self._ws.textMessageReceived.connect(self._on_text)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._stopped = False
        self.attempts = 0

    # ---- connection -------------------------------------------------------------------------------------------------------------
    def _get_connected(self) -> bool:
        return self._connected

    connected = Property(bool, _get_connected, notify=connectedChanged)

    def _set_connected(self, value: bool) -> None:
        if self._connected != value:
            self._connected = value
            self.connectedChanged.emit()

    def start(self) -> None:
        self._stopped = False
        self._open()

    def stop(self) -> None:
        self._stopped = True
        self._retry.stop()
        from PySide6.QtNetwork import QAbstractSocket

        if self._ws.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._ws.close()

    def _open(self) -> None:
        if self._stopped:
            return
        self.attempts += 1
        token = self._token_reader()
        if not token:                                       # Vex never ran yet: nothing to connect with
            self._schedule()
            return
        req = QNetworkRequest(QUrl(f"ws://127.0.0.1:{self.port}/ws"))
        req.setRawHeader(b"Authorization", b"Bearer " + token.encode("ascii", "ignore"))
        self._ws.open(req)

    def _schedule(self) -> None:
        if not self._stopped and not self._retry.isActive():
            self._retry.start(self.retry_ms)

    def _on_open(self) -> None:
        log.info("connected to Vex's local API")             # "hello" (sent after auth) flips `connected`

    def _on_closed(self) -> None:
        was = self._connected
        self._set_connected(False)
        pending, self._pending = self._pending, {}
        for cb in pending.values():
            self._safe(cb, False, None, "Vex disconnected")
        if was:
            log.info("lost Vex's local API; retrying every %.0f s", self.retry_ms / 1000)
        self._schedule()

    def _on_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if "event" in msg:
            if msg["event"] == "hello":
                self._set_connected(True)
                self.event.emit("state", (msg.get("data") or {}).get("state") or {})
            else:
                self.event.emit(str(msg["event"]), msg.get("data"))
            return
        cb = self._pending.pop(msg.get("id"), None) if isinstance(msg.get("id"), int) else None
        if cb is not None:
            self._safe(cb, bool(msg.get("ok")), msg.get("data"), str(msg.get("error") or ""))

    # ---- requests -----------------------------------------------------------------------------------------------------------------
    def call(self, cmd: str, args: dict | None = None, callback: Callback | None = None) -> int | None:
        if not self._connected:
            if callback is not None:
                self._safe(callback, False, None, "Vex isn't running")
            return None
        rid = next(self._ids)
        if callback is not None:
            self._pending[rid] = callback
        self._ws.sendTextMessage(json.dumps({"id": rid, "cmd": cmd, "args": args or {}}))
        return rid

    @staticmethod
    def _safe(cb: Callback, ok: bool, data: Any, error: str) -> None:
        try:
            cb(ok, data, error)
        except Exception:  # noqa: BLE001 - a view model bug must not break the connection
            log.exception("API callback failed")
