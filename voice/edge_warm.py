"""edge-tts over ONE long-lived connection, so a spoken sentence does not pay for a new connection every time.

edge-tts (the package) opens a fresh websocket for every sentence: measured here, about 0.7 s of handshake on top of about 0.4 s of
actual synthesis, so the first sound of a reply took roughly 1.4 s. The Edge service happily answers many requests on the same
websocket (a request on an open connection measured 0.3-0.6 s, still fine after 20 s idle). `WarmEdgeTTS` keeps one connection open
in a background thread, reconnects when it drops, and is pre-warmed at the start of every turn (`warm()`), so by the time the reply is
ready the handshake is long done.

It uses the same service, voices and credentials as edge-tts, through a few of its internal helpers. If those ever change in a new
edge-tts version, or anything fails twice, the plain per-sentence EdgeTTS takes over: warm is a speed-up, never a dependency.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Callable

from voice.tts import EdgeTTS, TTSError, decode_audio

log = logging.getLogger("vex.tts.warm")

MAX_BYTES = 3500                    # longer texts (the service limit is about 4 KB per request) go through the plain edge-tts splitter


class WarmEdgeTTS:
    name = "edge"

    def __init__(self, voice: str = "en-US-AriaNeural", fallback: EdgeTTS | None = None, rate: str = "+0%", volume: str = "+0%",
                 pitch: str = "+0Hz", connect_timeout: float = 8.0, request_timeout: float = 12.0, heartbeat: float = 15.0):
        self.voice = voice
        self.rate, self.volume, self.pitch = rate, volume, pitch
        self.fallback = fallback or EdgeTTS(voice)
        self.connect_timeout, self.request_timeout, self.heartbeat = connect_timeout, request_timeout, heartbeat
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._session = None
        self._ws = None
        self._lock: asyncio.Lock | None = None
        self.stats = {"warm_requests": 0, "reconnects": 0, "fallbacks": 0}
        self._broken = False                                  # an internals mismatch: stay on the plain path

    def available(self) -> bool:
        return True

    # ---- the background loop -------------------------------------------------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                started = threading.Event()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    started.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=run, name="edge-warm", daemon=True)
                self._thread.start()
                started.wait(2)
                self._loop = loop
            return self._loop

    def _submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    # ---- connection ------------------------------------------------------------------------------------------------------------
    async def _connect(self) -> None:
        import aiohttp
        from edge_tts.communicate import (_SSL_CTX, DRM, SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL, connect_id, date_to_string)

        await self._drop()
        self._session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.connect_timeout))
        url = (f"{WSS_URL}&ConnectionId={connect_id()}&Sec-MS-GEC={DRM.generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
        for attempt in (1, 2):
            try:
                self._ws = await self._session.ws_connect(url, compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_SSL_CTX,
                                                          heartbeat=self.heartbeat)
                break
            except aiohttp.ClientResponseError as e:
                if e.status == 403 and attempt == 1:             # the clock is off: the service tells us the time, then we retry
                    DRM.handle_client_response_error(e)
                    url = (f"{WSS_URL}&ConnectionId={connect_id()}&Sec-MS-GEC={DRM.generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
                    continue
                raise
        await self._ws.send_str(
            f"X-Timestamp:{date_to_string()}\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')
        log.info("edge-tts connection ready")

    async def _drop(self) -> None:
        ws, session, self._ws, self._session = self._ws, self._session, None, None
        for obj in (ws, session):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass

    async def _ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._ws is None or self._ws.closed:
                if self._ws is not None:
                    self.stats["reconnects"] += 1
                await self._connect()

    def warm(self) -> None:
        """Make sure the connection is open (non-blocking). Called at the start of each turn, so it is ready when the reply is."""
        if self._broken:
            return
        try:
            fut = self._submit(self._ensure())
            fut.add_done_callback(lambda f: f.exception() and log.debug("edge-tts warm-up failed: %s", f.exception()))
        except Exception:  # noqa: BLE001
            log.debug("edge-tts warm-up could not start", exc_info=True)

    # ---- speaking ------------------------------------------------------------------------------------------------------------------
    async def _speak(self, text: str) -> bytes:
        import aiohttp
        from edge_tts.communicate import (connect_id, date_to_string, escape, get_headers_and_data, mkssml, remove_incompatible_characters,
                                          ssml_headers_plus_data)
        from edge_tts.data_classes import TTSConfig

        await self._ensure()
        assert self._lock is not None
        async with self._lock:                                        # one request at a time on a connection
            tc = TTSConfig(self.voice, self.rate, self.volume, self.pitch, "SentenceBoundary")
            ws = self._ws
            await ws.send_str(ssml_headers_plus_data(connect_id(), date_to_string(), mkssml(tc, escape(remove_incompatible_characters(text)))))
            audio = bytearray()
            while True:
                msg = await asyncio.wait_for(ws.receive(), self.request_timeout)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if b"Path:turn.end" in msg.data.encode("utf-8", "replace"):
                        break
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    if len(msg.data) < 2:
                        continue
                    params, data = get_headers_and_data(msg.data, int.from_bytes(msg.data[:2], "big"))
                    if params.get(b"Path") == b"audio" and data:
                        audio += data
                else:                                                  # closed / error: this connection is no good any more
                    raise ConnectionError(f"edge-tts connection closed ({msg.type})")
            if not audio:
                raise TTSError("edge-tts returned no audio")
            return bytes(audio)

    def synthesize(self, text: str):
        if self._broken or len(text.encode("utf-8")) > MAX_BYTES:
            return self.fallback.synthesize(text)
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                data = self._submit(self._speak(text)).result(timeout=self.request_timeout + self.connect_timeout + 2)
                self.stats["warm_requests"] += 1
                return decode_audio(data)
            except ImportError as e:                                   # edge-tts changed its internals: plain path from now on
                log.warning("warm edge-tts is unavailable with this edge-tts version (%s); using the plain one", e)
                self._broken = True
                break
            except (FutureTimeout, Exception) as e:  # noqa: BLE001 - dropped connection, timeout, service hiccup
                last = e
                log.info("warm edge-tts request failed (attempt %d): %s", attempt, type(e).__name__)
                try:
                    self._submit(self._drop()).result(timeout=3)       # next attempt reconnects
                except Exception:  # noqa: BLE001
                    pass
        self.stats["fallbacks"] += 1
        if last is not None:
            log.info("warm edge-tts failed (%s); using the plain edge-tts for this sentence", type(last).__name__)
        return self.fallback.synthesize(text)

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            self._submit(self._drop()).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
