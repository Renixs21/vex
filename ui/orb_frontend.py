"""The marbled orb (vex_ui/, a separate GPU-rendered app) as a VisualFrontend.

Vex talks to it over UDP on localhost (JSON per datagram: {"state": ..., "level": 0.4, "caption": ..., "wake": true}), so a slow or
missing orb can never stall Vex: sends are fire-and-forget and errors are swallowed. This module has four parts:

  OrbFrontend   implements ui.frontend.VisualFrontend: state, level (30 Hz), caption, show (wake) / hide (sleep)
  OrbProcess    starts the orb app (`pythonw -m vex_ui.app`) if it isn't already running, and closes it again on quit
  ClickListener the orb sends {"click": true} (overlay) or {"event": "wake_clicked"} (dock pill) to a second UDP port; this turns that
                into controller.start_listening("click")
  MultiFrontend fans every call out to several frontends (for example the orb and the small Qt pill together)
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ui.frontend import STATES, VisualFrontend, clamp01

log = logging.getLogger("vex.orb")

ORB_PORT = 47474
CLICK_PORT = 47475
MAX_CAPTION = 160


def short_caption(text: str, limit: int = MAX_CAPTION) -> str:
    """Captions on the orb wrap over a few lines; keep long replies to a readable length (the full text stays in the pill/log)."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    return text[: cut if cut > limit // 2 else limit - 1].rstrip(",;:") + "…"


class OrbFrontend:
    def __init__(self, port: int = ORB_PORT, host: str = "127.0.0.1"):
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._last_level: float | None = None
        self._first_level_logged = False
        self._lock = threading.Lock()

    def _send(self, **msg) -> None:
        try:
            with self._lock:
                self._sock.sendto(json.dumps(msg, ensure_ascii=False).encode("utf-8"), self.addr)
        except OSError:                                              # nothing listening, network stack hiccup: never matters
            log.debug("orb send failed", exc_info=True)

    # ---- VisualFrontend ---------------------------------------------------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state in STATES:
            self._last_level = None
            self._first_level_logged = False
            log.info("orb: state -> %s", state)
            self._send(state=state)

    def set_level(self, level: float) -> None:
        v = round(clamp01(level), 3)
        if v != self._last_level:                                    # drop repeats (silence sends 0.0 at 30 Hz)
            self._last_level = v
            if not self._first_level_logged:
                self._first_level_logged = True
                log.info("orb: first level sent this turn: %.3f", v)
            self._send(level=v)

    def show_caption(self, text: str) -> None:
        self._send(caption=short_caption(text))

    def show(self) -> None:
        self._send(wake=True, state="idle")

    def hide(self) -> None:
        self._send(state="sleep")

    # ---- extras the orb supports ------------------------------------------------------------------------------------------
    def set_palette(self, name: str) -> None:
        self._send(palette=name)

    def quit(self) -> None:
        self._send(quit=True)


class MultiFrontend:
    """Sends every call to each frontend; one failing never affects the others."""

    def __init__(self, frontends: list[VisualFrontend]):
        self.frontends = frontends

    def _each(self, name: str, *args) -> None:
        for f in self.frontends:
            try:
                getattr(f, name)(*args)
            except Exception:  # noqa: BLE001
                log.exception("frontend %s.%s failed", type(f).__name__, name)

    def set_state(self, state: str) -> None:
        self._each("set_state", state)

    def set_level(self, level: float) -> None:
        self._each("set_level", level)

    def show_caption(self, text: str) -> None:
        self._each("show_caption", text)

    def show(self) -> None:
        self._each("show")

    def hide(self) -> None:
        self._each("hide")


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


class OrbProcess:
    """Starts the orb app from the vex_ui folder. If something already listens on its port it is left alone (and not stopped later)."""

    def __init__(self, folder: Path, port: int = ORB_PORT, click_port: int = CLICK_PORT, mode: str = "dock",
                 position: str = "bottom", palette: str = "blue", size: str = "", autosleep: float = 0.0,
                 edge: str = "right", edge_y: float = 0.40, center_y: float = 0.50, orb_radius: float = 62.0,
                 python: str | None = None, popen: Callable = subprocess.Popen,
                 port_in_use: Callable[[int], bool] = _port_in_use):
        self.folder = Path(folder)
        self.port, self.click_port = port, click_port
        self.args = ["--mode", mode, "--palette", palette, "--port", str(port)]
        if mode == "dock":              # the pill on the screen edge; clicking it reports {"event": "wake_clicked"}
            self.args += ["--notify-port", str(click_port), "--edge", edge, "--edge-y", str(edge_y),
                          "--center-y", str(center_y), "--orb-radius", str(orb_radius)]
        else:
            self.args += ["--position", position, "--click-port", str(click_port)]
        if size and mode != "dock":
            self.args += ["--size", size]
        if autosleep:
            self.args += ["--autosleep", str(autosleep)]
        exe = Path(python or sys.executable)
        pyw = exe.with_name("pythonw.exe")
        self.python = str(pyw if pyw.exists() else exe)            # pythonw: no console window
        self._popen = popen
        self._in_use = port_in_use
        self.proc = None
        self.started_by_us = False
        self._stopping = False
        self._watch_thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return (self.folder / "vex_ui" / "app.py").is_file()

    def command(self) -> list[str]:
        return [self.python, "-m", "vex_ui.app", *self.args]

    def start(self, wait: float = 10.0) -> bool:
        """Make sure an orb is running. Returns False if it can't be started (missing folder, GPU/driver trouble)."""
        if self._in_use(self.port):
            log.info("an orb is already listening on %d", self.port)
            return True
        if not self.available:
            log.warning("orb app not found in %s", self.folder)
            return False
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = self._popen(self.command(), cwd=str(self.folder), creationflags=creation,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("could not start the orb: %s", e)
            return False
        self.started_by_us = True
        end = time.monotonic() + wait
        while time.monotonic() < end:                                # the orb binds its port once its window and GPU are ready
            if self._in_use(self.port):
                return True
            if self.proc.poll() is not None:
                log.warning("the orb exited immediately (code %s); check the GPU driver / run vex_ui manually to see the error",
                            self.proc.returncode)
                return False
            time.sleep(0.1)
        log.warning("the orb didn't start listening within %.0f s", wait)
        return False

    def crashed(self) -> bool:
        """True if an orb that we started has died without being asked to."""
        return self.started_by_us and not self._stopping and self.proc is not None and self.proc.poll() is not None

    def watch(self, on_giveup: Callable[[], None], budget=None, interval: float = 5.0, on_restart: Callable[[], None] | None = None) -> None:
        """Restart the orb if it dies: at most 3 times in 10 minutes, then `on_giveup()` (the caller falls back to the pill and says so)."""
        from ui.recovery import ComponentWatch, RestartBudget

        w = ComponentWatch("the orb", lambda: not self.crashed(), lambda: (self.start(), on_restart and on_restart()),
                           lambda name: on_giveup(), budget=budget or RestartBudget())
        self.watcher = w

        def loop() -> None:
            while not self._stopping:
                time.sleep(interval)
                if self._stopping or w.check() == "gave_up":
                    return

        self._watch_thread = threading.Thread(target=loop, name="orb-watch", daemon=True)
        self._watch_thread.start()

    def stop(self) -> None:
        """Close an orb that we started (never one the user launched themselves)."""
        self._stopping = True
        if not self.started_by_us or self.proc is None:
            return
        try:
            OrbFrontend(self.port).quit()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None


class ClickListener:
    """UDP listener for the orb's {"click": true}: calls on_click("orb-click"). Ignores anything else."""

    def __init__(self, port: int, on_click: Callable[[str], object], host: str = "127.0.0.1",
                 on_open: Callable[[], object] | None = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.on_click = on_click
        self.on_open = on_open                    # a double-click on the pill: open the Control Center
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="orb-clicks", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            raw = data[:120]
            try:
                msg = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.info("click port %d: datagram from %s:%d %r -> ignored (not JSON)", self.port, addr[0], addr[1], raw)
                continue
            if isinstance(msg, dict) and msg.get("event") == "open_control_center":
                log.info("click port %d: double-click on the pill -> opening the Control Center", self.port)
                if self.on_open is not None:
                    try:
                        self.on_open()
                    except Exception:  # noqa: BLE001
                        log.exception("opening the Control Center failed")
                continue
            if isinstance(msg, dict) and (msg.get("click") is True or msg.get("event") == "wake_clicked"):   # overlay / dock
                log.info("click port %d: datagram from %s:%d %r -> orb click, starting a listening turn", self.port, addr[0], addr[1], raw)
                try:
                    self.on_click("orb-click")
                except Exception:  # noqa: BLE001
                    log.exception("click handler failed")
            else:
                log.info("click port %d: datagram from %s:%d %r -> ignored (not a click)", self.port, addr[0], addr[1], raw)
