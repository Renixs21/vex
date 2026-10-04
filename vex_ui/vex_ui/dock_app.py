"""
Vex dock window: the orb lives as a thin pill on a screen edge (like Samsung's Edge Panel handle).
"Hey Vex" -> it peels off, swells into the orb and glides to the middle of the screen.
"Sleep"   -> it shrinks and slides back to the edge.

One small (560x350) transparent, always-on-top window that moves with the shape. Only the pill / orb
itself receives the mouse (via a window mask); everything else clicks straight through. The window never
covers the screen, so Windows never treats it as a fullscreen app and only a small area is composited.
Used by `python -m vex_ui.app` (the default mode).
"""
from __future__ import annotations

import ctypes
import json
import logging
import socket
import sys
import time

import moderngl
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from vex_ui.app import ControlServer, Mic, orb_icon
from vex_ui.dock import DockConfig, DockRenderer
from vex_ui.renderer import PALETTES, STATES


log = logging.getLogger("vex_ui")
VIEW = (560, 350)                    # logical size of the window that follows the shape


class DockWindow(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus)          # clicking Vex never steals focus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Vex")
        self.setWindowIcon(orb_icon())

        screens = QGuiApplication.screens()
        screen = screens[args.screen] if 0 <= args.screen < len(screens) else QGuiApplication.primaryScreen()
        sg = screen.geometry()
        self.sx, self.sy = sg.x(), sg.y()                  # this monitor's top-left on the virtual desktop
        self.dpr = float(screen.devicePixelRatio())
        self.setGeometry(sg.x() + sg.width() - VIEW[0], sg.y() + int(sg.height() * args.edge_y) - 112, *VIEW)
        self._frames, self._frame_ms, self._dbg_t = 0, 0.0, time.perf_counter()

        try:
            self.ctx = moderngl.create_standalone_context()
        except Exception:                                  # headless Linux fallback
            self.ctx = moderngl.create_standalone_context(backend="egl")
        cfg = DockConfig(edge=args.edge, edge_y=args.edge_y, center_y=args.center_y,
                         orb_radius=args.orb_radius, speed=args.speed)
        self.r = DockRenderer(self.ctx, (sg.width(), sg.height()), self.dpr, cfg, view_size=VIEW)
        self.r.set_palette(args.palette)

        self.server = ControlServer(args.port)
        self.server.start()
        self.notify = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.mic = Mic() if args.mic else None
        self._img = None
        self._img_rect = QRectF()
        self._solid = True                                 # becomes click-through on the first frame
        self.start = self.last = time.perf_counter()
        self.last_active = self.start

        if args.start_awake:
            self.r.set_caption("What can I do for you today?")
            self.r.set_state("idle")
            self.r.s.s = self.r.s.target = 1.0

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._on_timer)
        self.timer.start(int(1000 / args.fps))
        self._build_menu()
        print(f"[vex-ui] dock mode ({args.edge} edge) — control udp://127.0.0.1:{args.port}, "
              f"events udp://127.0.0.1:{args.notify_port}")

    # ------------------------------------------------------------ menu / tray
    def _build_menu(self):
        self.menu = QMenu()
        self.menu.addAction("Wake  (Hey Vex)").triggered.connect(self.do_wake)
        self.menu.addAction("Sleep  (dock to edge)").triggered.connect(lambda: self.set_state("sleep"))
        self.menu.addSeparator()
        for st in STATES[:-1]:
            self.menu.addAction(st.capitalize()).triggered.connect(lambda _=False, s=st: self.set_state(s))
        self.menu.addSeparator()
        pal = self.menu.addMenu("Colour")
        for name in PALETTES:
            pal.addAction(name.capitalize()).triggered.connect(lambda _=False, n=name: self.r.set_palette(n))
        self.menu.addAction("Quit Vex UI").triggered.connect(QApplication.quit)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(orb_icon(), self)
            self.tray.setToolTip("Vex")
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(
                lambda reason: self._clicked_wake() if reason == QSystemTrayIcon.Trigger and self.r.docked else None)
            self.tray.show()

    # ------------------------------------------------------------ actions
    def do_wake(self):
        self.r.wake()
        if not self.r.s.caption:
            self.r.set_caption("What can I do for you today?")
        self.last_active = time.perf_counter()

    def set_state(self, st):
        if st == "sleep":
            self.r.set_state("sleep")
        else:
            if self.r.docked:
                self.do_wake()
            self.r.set_state(st)
        self.last_active = time.perf_counter()

    def _clicked_wake(self):
        """User woke Vex by clicking the pill / tray icon: animate, and tell Vex's backend."""
        self.do_wake()
        try:
            self.notify.sendto(json.dumps({"event": "wake_clicked"}).encode(),
                               ("127.0.0.1", self.args.notify_port))
        except OSError:
            pass

    # ------------------------------------------------------------ frame loop
    def _on_timer(self):
        now = time.perf_counter()
        t, dt = now - self.start, min(0.05, now - self.last)
        self.last = now
        self.step(t, dt)

    def step(self, t: float, dt: float):
        r, now = self.r, time.perf_counter()
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

        r.update(t, dt)
        ox, oy = r.view_origin()                           # the window follows the shape
        r.ox, r.oy = ox, oy
        if (self.x() - self.sx, self.y() - self.sy) != (ox, oy):
            self.move(self.sx + ox, self.sy + oy)
        data, (x, y, w, h) = r.render()
        d = self.dpr
        self._data = data                                    # GL rows are bottom-first; paintEvent flips them (no copy)
        self._img = QImage(self._data, w, h, w * 4, QImage.Format_RGBA8888_Premultiplied)
        self.update()
        self._update_click_through()
        if self.args.debug:
            self._frames += 1
            self._frame_ms += (time.perf_counter() - now) * 1000
            if now - self._dbg_t >= 2.0:
                fps = self._frames / (now - self._dbg_t)
                msg = f"fps {fps:.1f}  {self._frame_ms / self._frames:.1f} ms/frame  dpr {d}"
                print("[vex-ui]", msg, flush=True)
                log.info(msg)
                self._frames, self._frame_ms, self._dbg_t = 0, 0.0, now

    def _draw_caption(self, p: QPainter, alpha: float):
        cx, cy, w, h = self.r.caption_rect()
        cx, cy = cx - self.r.ox, cy - self.r.oy             # screen -> window coordinates
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        font = QFont("Segoe UI", 15)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        flags = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
        box = QRectF(cx, cy, w, h)
        p.setOpacity(alpha)
        shadow = QColor(4, 8, 40, 150)                       # soft dark halo so it reads on any wallpaper
        for dx, dy in ((-1.5, 0), (1.5, 0), (0, -1.5), (0, 1.5), (0, 2.5), (-1, 1), (1, 1)):
            p.setPen(QPen(shadow))
            p.drawText(box.translated(dx, dy), int(flags), self.r.s.caption)
        p.setPen(QColor(246, 248, 255))
        p.drawText(box, int(flags), self.r.s.caption)
        p.setOpacity(1.0)

    def _update_click_through(self):
        """Clicks pass through the window everywhere except on the pill / orb.

        A window mask would also clip the glow and the caption, so instead the window is made click-through
        (WS_EX_TRANSPARENT) unless the cursor is over the shape; checked every frame."""
        (x, y, w, h), is_ball = self.r.hit_rect()
        p = QCursor.pos()
        lx, ly = p.x() - self.sx - x, p.y() - self.sy - y        # hit_rect is in screen coordinates
        inside = 0 <= lx <= w and 0 <= ly <= h
        if inside and is_ball:                              # the ball is round
            nx, ny = lx / (w / 2) - 1, ly / (h / 2) - 1
            inside = nx * nx + ny * ny <= 1
        if inside != self._solid:
            self._solid = inside
            self._set_transparent(not inside)

    def _set_transparent(self, on: bool):
        if sys.platform != "win32":
            return
        try:
            GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x20
            hwnd = int(self.winId())
            u32 = ctypes.windll.user32
            u32.GetWindowLongW.restype = ctypes.c_long
            style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:                                   # noqa: BLE001 - never break the animation over this
            log.exception("could not change click-through")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(e.rect(), Qt.transparent)
        if self._img is not None:
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.save()
            p.translate(0, VIEW[1])
            p.scale(VIEW[0] / self._img.width(), -VIEW[1] / self._img.height())    # flip: GL rows are bottom-first
            p.drawImage(0, 0, self._img)
            p.restore()
            ca = self.r.caption_alpha()
            if ca > 0.003:
                self._draw_caption(p, ca)
        p.end()

    # ------------------------------------------------------------ input
    def enterEvent(self, e):
        self.r.set_hover(True)

    def leaveEvent(self, e):
        self.r.set_hover(False)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.r.docked:
            self._clicked_wake()
        elif e.button() == Qt.RightButton:
            self.menu.popup(e.globalPosition().toPoint())
