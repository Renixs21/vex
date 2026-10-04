"""The local API: one websocket on 127.0.0.1 (never another interface), JSON messages, a token on every connection.

    ws://127.0.0.1:47480/ws          token: the text in data/api_token (created on first start, never logged, never in git)

Auth: send the header `Authorization: Bearer <token>`, or make the FIRST message {"type": "auth", "token": "..."} within 5 s.
Anything else is closed (4401). Wrong tokens are slowed down, and after 10 bad tries in a minute every new connection is refused
for a minute. Requests whose Host isn't 127.0.0.1/localhost are refused (DNS rebinding), and so are browser pages (an Origin header)
unless the origin is listed in [api] allowed_origins.

Messages (client -> Vex):  {"id": 1, "cmd": "text", "args": {"text": "what's the time"}}
Replies:                    {"id": 1, "ok": true, "data": ...}   or   {"id": 1, "ok": false, "error": "..."}
Pushed events:              {"event": "state" | "tool" | "confirm" | "confirm_done" | "activity", "data": {...}}
Commands: see ui/api_core.py and docs/API.md. There is no command to answer a confirmation: those happen on screen in Vex.

The server runs on its own thread with its own event loop; nothing in Vex waits for it, and with no client connected it only idles.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from ui.api_core import ApiCore, ApiError

log = logging.getLogger("vex.api")

AUTH_TIMEOUT = 5.0
MAX_MESSAGE = 64_000
MAX_CLIENTS = 8
BAD_TRIES = 10
LOCKOUT_S = 60.0


def load_or_create_token(path: Path) -> str:
    """The shared secret: data/api_token. Created once with 256 random bits. Never logged."""
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tok, encoding="utf-8")
    os.replace(tmp, path)
    log.info("created a new local API token in %s", path)
    return tok


class ApiServer:
    def __init__(self, core: ApiCore, token: str, host: str = "127.0.0.1", port: int = 47480, allowed_origins: list[str] | None = None,
                 clock=time.monotonic):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("the local API only listens on 127.0.0.1")
        self.core = core
        self._token = token
        self.host, self.port = "127.0.0.1", port
        self.allowed_origins = set(allowed_origins or [])
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner = None
        self._clients: set = set()
        self._bad: deque[float] = deque()
        self._started = threading.Event()
        self.error: str | None = None
        self.bound_port: int | None = None

    # ---- lifecycle ------------------------------------------------------------------------------------------------------------
    def start(self, wait: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="local-api", daemon=True)
        self._thread.start()
        self._started.wait(wait)
        if self.error:
            log.warning("local API not started: %s", self.error)
            return False
        self.core.subscribers.append(self.broadcast)
        log.info("local API listening on ws://127.0.0.1:%s/ws (token in data/api_token)", self.bound_port)
        return True

    def stop(self) -> None:
        if self.broadcast in self.core.subscribers:
            self.core.subscribers.remove(self.broadcast)
        loop = self._loop
        if loop is None:
            return

        async def shutdown():
            for ws in list(self._clients):
                await ws.close()
            if self._runner is not None:
                await self._runner.cleanup()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)

    def _run(self) -> None:
        from aiohttp import web

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        app = web.Application(client_max_size=MAX_MESSAGE)
        app.router.add_get("/ws", self._handle)
        runner = web.AppRunner(app, access_log=None)                 # no access log: nothing about clients (or tokens) in vex.log
        self._runner = runner
        try:
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", self.port, reuse_address=False)
            loop.run_until_complete(site.start())
            sock = site._server.sockets[0] if site._server and site._server.sockets else None   # noqa: SLF001 - to learn port 0's choice
            self.bound_port = sock.getsockname()[1] if sock else self.port
        except Exception as e:  # noqa: BLE001 - port taken etc.: Vex runs on without the API
            self.error = f"{type(e).__name__}: {e}"
            self._started.set()
            return
        self._started.set()
        loop.run_forever()

    # ---- connections ------------------------------------------------------------------------------------------------------------
    def _locked_out(self) -> bool:
        now = self._clock()
        while self._bad and now - self._bad[0] > LOCKOUT_S:
            self._bad.popleft()
        return len(self._bad) >= BAD_TRIES

    def _check(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(str(given).encode(), self._token.encode())

    async def _reject(self, ws, why: str) -> None:
        self._bad.append(self._clock())
        log.warning("local API: refused a connection (%s)", why)                 # never the token itself
        await asyncio.sleep(0.5)                                               # slows down guessing
        await ws.close(code=4401, message=b"unauthorized")

    async def _handle(self, request):
        from aiohttp import WSMsgType, web

        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return web.Response(status=403, text="forbidden")
        origin = request.headers.get("Origin")
        if origin and origin not in self.allowed_origins:
            log.warning("local API: refused a browser connection from %s", origin)
            return web.Response(status=403, text="forbidden")
        if self._locked_out():
            return web.Response(status=429, text="too many bad tokens; try again in a minute")
        if len(self._clients) >= MAX_CLIENTS:
            return web.Response(status=503, text="too many clients")
        ws = web.WebSocketResponse(max_msg_size=MAX_MESSAGE, heartbeat=30)
        await ws.prepare(request)
        auth = request.headers.get("Authorization", "")
        ok = auth.startswith("Bearer ") and self._check(auth[7:].strip())
        if not ok and auth:                                            # a wrong header: refused at once, no second chance
            await self._reject(ws, "wrong token")
            return ws
        if not ok:
            try:
                msg = await asyncio.wait_for(ws.receive(), AUTH_TIMEOUT)
                first = json.loads(msg.data) if msg.type == WSMsgType.TEXT else {}
                ok = isinstance(first, dict) and first.get("type") == "auth" and self._check(first.get("token"))
            except (asyncio.TimeoutError, ValueError, TypeError):
                ok = False
        if not ok:
            await self._reject(ws, "missing or wrong token")
            return ws
        self._clients.add(ws)
        log.info("local API: client connected (%d connected)", len(self._clients))
        await ws.send_json({"event": "hello", "data": {"commands": sorted(self.core.commands), "state": self.core.state({})}})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                await self._dispatch(ws, msg.data)
        finally:
            self._clients.discard(ws)
            log.info("local API: client disconnected (%d connected)", len(self._clients))
        return ws

    async def _dispatch(self, ws, raw: str) -> None:
        try:
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            await ws.send_json({"ok": False, "error": "send JSON objects: {\"id\": 1, \"cmd\": \"state\", \"args\": {}}"})
            return
        rid, cmd, args = req.get("id"), str(req.get("cmd", "")), req.get("args") or {}
        fn = self.core.commands.get(cmd)
        if fn is None or not isinstance(args, dict):
            await ws.send_json({"id": rid, "ok": False, "error": f"unknown command {cmd!r}" if fn is None else "args must be an object"})
            return
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, fn, args)      # Vex's own code never runs on this loop
            reply = {"id": rid, "ok": True, "data": data}
        except ApiError as e:
            reply = {"id": rid, "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("local API command %s failed", cmd)
            reply = {"id": rid, "ok": False, "error": f"{cmd} failed: {type(e).__name__}"}
        if ws.closed:                                                   # the client left while the command ran: nobody to tell
            return
        try:
            await ws.send_json(reply, dumps=lambda o: json.dumps(o, default=str))
        except (ConnectionError, RuntimeError) as e:                   # it left just now
            log.debug("local API: client gone before the reply (%s)", type(e).__name__)

    # ---- events ---------------------------------------------------------------------------------------------------------------------
    def broadcast(self, event: dict) -> None:
        """Thread-safe; returns at once. With no client connected it does nothing at all."""
        loop = self._loop
        if loop is None or not self._clients:
            return
        text = json.dumps(event, default=str)

        async def send():
            for ws in list(self._clients):
                try:
                    await ws.send_str(text)
                except Exception:  # noqa: BLE001
                    self._clients.discard(ws)

        try:
            asyncio.run_coroutine_threadsafe(send(), loop)
        except RuntimeError:
            pass
