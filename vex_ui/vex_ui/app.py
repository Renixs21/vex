"""
Vex visual — desktop app for Windows (also runs on macOS/Linux).

    python -m vex_ui.app                      # overlay: orb floats on the desktop, bottom-centre
    python -m vex_ui.app --mode window        # widescreen window, dark background
    python -m vex_ui.app --palette violet     # blue (default) | default (grey) | violet | emerald
    python -m vex_ui.app --mode window --fullscreen
    python -m vex_ui.app --mic --autosleep 20 # react to mic; fade away after 20 s idle

Overlay: a small marbled orb, transparent background, always on top, no taskbar button. Empty areas are click-through;
drag the orb to move it. Right-click the orb or use the tray icon for the menu.
Keys (when focused): 1 idle · 2 listening · 3 thinking · 4 speaking · 5 sleep · W wake ·
F11 fullscreen (window mode) · Esc quit (window mode) / sleep (overlay).

Vex's backend controls it over localhost UDP — see vex_ui/client.py.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time

import moderngl
from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from vex_ui.renderer import PALETTES, STATES, VexRenderer

PORT = 47474


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Vex orb visual")
    ap.add_argument("--mode", choices=["overlay", "window"], default="overlay")
    ap.add_argument("--size", default=None, help="logical size, e.g. 560x440 (overlay) or 1280x720 (window)")
    ap.add_argument("--position", default="bottom",
                    choices=["bottom", "bottom-right", "bottom-left", "center", "top-right"],
                    help="where the overlay sits on screen")
    ap.add_argument("--fullscreen", action="store_true", help="window mode: start fullscreen")
    ap.add_argument("--autosleep", type=float, default=0, help="seconds idle before fading away (0 = never)")
    ap.add_argument("--palette", choices=list(PALETTES), default="blue", help="orb colour")
    ap.add_argument("--mic", action="store_true", help="orb reacts to your microphone while listening")
    ap.add_argument("--start-asleep", action="store_true", help="start hidden; appear on wake()")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--click-port", type=int, default=0,
                    help="send {\"click\": true} to udp://127.0.0.1:<port> when the orb is clicked (not dragged); 0 = off")
    ap.add_argument("--fps", type=int, default=60)
    return ap.parse_args(argv)


# ---------------------------------------------------------------- inputs
class Mic:
    """Optional microphone level (pip install sounddevice)."""
    def __init__(self):
        self.level = 0.0
        self.stream = None
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            print("[vex-ui] --mic needs: pip install sounddevice")
            return

        def cb(indata, frames, t, status):
            self.level = min(1.0, float(np.sqrt(np.mean(indata[:, 0] ** 2))) * 18.0)

        self.stream = sd.InputStream(channels=1, samplerate=16000, blocksize=512, callback=cb)
        self.stream.start()


class ControlServer(threading.Thread):
    """JSON over localhost UDP: {"state": "...", "level": 0.4, "caption": "...", "wake": true}"""
    def __init__(self, port):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.inbox: list[dict] = []
        self.lock = threading.Lock()

    def run(self):
        while True:
            data, _ = self.sock.recvfrom(65536)
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            with self.lock:
                self.inbox.append(msg)

    def drain(self):
        with self.lock:
            msgs, self.inbox = self.inbox, []
        return msgs


def orb_icon(size=64) -> QIcon:
    """Small tray icon: a softly lit blue sphere."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    tri = QPainterPath()
    tri.addEllipse(QPointF(s * 0.5, s * 0.5), s * 0.42, s * 0.42)
    g = QRadialGradient(QPointF(s * 0.38, s * 0.62), s * 0.55)
    g.setColorAt(0.0, QColor(215, 235, 255))
    g.setColorAt(0.45, QColor(95, 140, 220))
    g.setColorAt(1.0, QColor(40, 70, 150))
    p.fillPath(tri, g)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- window
class VexWindow(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.mode = args.mode
        overlay = self.mode == "overlay"

        if args.size:
            w, h = (int(v) for v in args.size.lower().split("x"))
        else:
            w, h = (420, 340) if overlay else (1280, 720)

        flags = Qt.FramelessWindowHint
        if overlay:
            flags |= Qt.WindowStaysOnTopHint | Qt.Tool          # no taskbar button
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowFlags(flags)
        self.setWindowTitle("Vex")
        self.setWindowIcon(orb_icon())
        self.resize(w, h)
        self._place()

        # GPU renderer in an off-screen context; frames are handed to Qt as premultiplied images
        try:
            self.ctx = moderngl.create_standalone_context()
        except Exception:      # headless Linux fallback
            self.ctx = moderngl.create_standalone_context(backend="egl")
        self.fbo = None
        self.r = None
        self._ensure_gpu()

        self.server = ControlServer(args.port)
        self.server.start()
        self.mic = Mic() if args.mic else None
        self.image = None
        self.start = self.last = time.perf_counter()
        self.last_active = self.start

        self.r.set_palette(args.palette)
        if args.start_asleep:
            self.r.set_state("sleep")
            self.r.s.presence = 0.0
        else:
            self.r.set_caption("What can I do for you today?")
            self.r.wake()

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / args.fps))

        self._build_menu()
        print(f"[vex-ui] {self.mode} mode — control port udp://127.0.0.1:{args.port}")

    # ----- layout -----
    def _place(self):
        scr = QGuiApplication.primaryScreen().availableGeometry()   # excludes the taskbar
        w, h = self.width(), self.height()
        if self.mode == "window":
            x, y = scr.center().x() - w // 2, scr.center().y() - h // 2
        else:
            m = 16
            x, y = {
                "bottom": (scr.center().x() - w // 2, scr.bottom() - h + 1),
                "bottom-right": (scr.right() - w - m, scr.bottom() - h + 1),
                "bottom-left": (scr.left() + m, scr.bottom() - h + 1),
                "center": (scr.center().x() - w // 2, scr.center().y() - h // 2),
                "top-right": (scr.right() - w - m, scr.top() + m),
            }[self.args.position]
        self.move(x, y)

    def _phys_size(self):
        dpr = self.devicePixelRatioF()
        return max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))

    def _ensure_gpu(self):
        size = self._phys_size()
        if self.fbo is not None and self.fbo.size == size:
            return
        if self.fbo is not None:
            self.fbo.release()
        self.fbo = self.ctx.simple_framebuffer(size, components=4)
        if self.r is None:
            self.r = VexRenderer(self.ctx, size, mode=self.mode, flip=True)
        else:
            self.r.resize(size)

    # ----- menu / tray -----
    def _build_menu(self):
        self.menu = QMenu()
        act = self.menu.addAction("Wake  (Hey Vex)")
        act.triggered.connect(self.do_wake)
        self.menu.addSeparator()
        for st in STATES:
            a = self.menu.addAction(st.capitalize())
            a.triggered.connect(lambda _=False, s=st: self.set_state(s))
        self.menu.addSeparator()
        pal = self.menu.addMenu("Colour")
        for name in PALETTES:
            pal.addAction(name.capitalize()).triggered.connect(lambda _=False, n=name: self.r.set_palette(n))
        self.menu.addAction("Recentre").triggered.connect(self._place)
        if self.mode == "window":
            self.menu.addAction("Toggle fullscreen  (F11)").triggered.connect(self.toggle_full)
        self.menu.addAction("Quit Vex UI").triggered.connect(QApplication.quit)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(orb_icon(), self)
            self.tray.setToolTip("Vex")
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(
                lambda reason: self.do_wake() if reason == QSystemTrayIcon.Trigger and self.r.asleep else None)
            self.tray.show()

    # ----- actions -----
    def do_wake(self):
        self.r.wake()
        if not self.r.s.caption:
            self.r.set_caption("What can I do for you today?")
        self.last_active = time.perf_counter()

    def set_state(self, st):
        if st == "sleep":
            self.r.set_state("sleep")
        else:
            if self.r.s.state == "sleep":
                self.r.wake()
            self.r.set_state(st)
        self.last_active = time.perf_counter()

    def toggle_full(self):
        if self.mode != "window":
            return
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ----- frame loop -----
    def tick(self):
        now = time.perf_counter()
        t, dt = now - self.start, min(0.05, now - self.last)
        self.last = now
        r = self.r

        for msg in self.server.drain():
            if msg.get("quit"):
                QApplication.quit(); return
            if msg.get("wake"):
                self.do_wake()
            if "state" in msg:
                try:
                    self.set_state(msg["state"])
                except ValueError as ex:
                    print("[vex-ui]", ex)
            if "level" in msg:
                r.set_level(msg["level"], t)
            if "caption" in msg:
                r.set_caption(msg["caption"] or "")
            if "palette" in msg:
                try:
                    r.set_palette(msg["palette"])
                except ValueError as ex:
                    print("[vex-ui]", ex)
            self.last_active = now

        if self.mic and r.s.state == "listening":
            r.set_level(self.mic.level, t)
        if r.s.state not in ("idle", "sleep"):
            self.last_active = now
        if self.args.autosleep and r.s.state == "idle" and now - self.last_active > self.args.autosleep:
            r.set_state("sleep")

        # fully asleep overlay: skip GPU work, keep window fully transparent (and click-through)
        if self.mode == "overlay" and r.asleep and self.image is not None and getattr(self, "_blank", False):
            r.update(t, dt)
            return

        self._ensure_gpu()
        self.fbo.use()
        self.fbo.clear(0.0, 0.0, 0.0, 0.0)
        r.render(t, dt)
        w, h = self.fbo.size
        self._buf = self.fbo.read(components=4)
        fmt = QImage.Format_RGBA8888_Premultiplied if self.mode == "overlay" else QImage.Format_RGBA8888
        img = QImage(self._buf, w, h, w * 4, fmt)
        img.setDevicePixelRatio(self.devicePixelRatioF())
        self.image = img
        self._blank = r.asleep
        self.update()

    def paintEvent(self, _):
        if self.image is None:
            return
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(0, 0, self.image)
        p.end()

    # ----- input -----
    def _check_click(self, press_pos):
        """A press that neither moved the window nor is still held was a click (the system move swallows the release)."""
        if self.pos() == press_pos and not (QApplication.mouseButtons() & Qt.LeftButton) and self.args.click_port:
            try:
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'{"click": true}', ("127.0.0.1", self.args.click_port))
            except OSError:
                pass

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.windowHandle():
            if self.args.click_port:
                press_pos = self.pos()
                QTimer.singleShot(260, lambda: self._check_click(press_pos))
            self.windowHandle().startSystemMove()
        elif e.button() == Qt.RightButton:
            self.menu.popup(e.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, e):
        if self.mode == "window":
            self.toggle_full()

    def keyPressEvent(self, e):
        k = e.key()
        keys = {Qt.Key_1: "idle", Qt.Key_2: "listening", Qt.Key_3: "thinking",
                Qt.Key_4: "speaking", Qt.Key_5: "sleep"}
        if k in keys:
            self.set_state(keys[k])
        elif k == Qt.Key_W:
            self.do_wake()
        elif k == Qt.Key_F11:
            self.toggle_full()
        elif k == Qt.Key_Escape:
            if self.mode == "window":
                QApplication.quit()
            else:
                self.set_state("sleep")

    def resizeEvent(self, e):
        self.image = None
        super().resizeEvent(e)


def main(argv=None):
    args = parse_args(argv)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName("Vex")
    w = VexWindow(args)
    if args.mode == "window" and args.fullscreen:
        w.showFullScreen()
    else:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
