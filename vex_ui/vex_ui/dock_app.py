"""
Vex dock window: the orb lives as a thin pill on a screen edge (like Samsung's Edge Panel handle).
"Hey Vex" -> it peels off, swells into the orb and glides to the middle of the screen.
"Sleep"   -> it shrinks and slides back to the edge.

One transparent, click-through, always-on-top window covers the screen. Only the pill / orb
itself receives the mouse (via a window mask); everything else clicks straight through.
Used by `python -m vex_ui.app` (the default mode).
"""
from __future__ import annotations

import json
import socket
import time

import moderngl
from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QRegion
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from vex_ui.app import ControlServer, Mic, orb_icon
from vex_ui.dock import DockConfig, DockRenderer
from vex_ui.renderer import PALETTES, STATES


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
        # one pixel short of the full height: Windows hides the taskbar for borderless windows that
        # cover a whole monitor, and this keeps it from treating Vex as a fullscreen app
        self.setGeometry(screen.geometry().adjusted(0, 0, 0, -1))
        self.dpr = float(screen.devicePixelRatio())

        try:
            self.ctx = moderngl.create_standalone_context()
        except Exception:                                  # headless Linux fallback
            self.ctx = moderngl.create_standalone_context(backend="egl")
        cfg = DockConfig(edge=args.edge, edge_y=args.edge_y, center_y=args.center_y,
                         orb_radius=args.orb_radius, speed=args.speed)
        self.r = DockRenderer(self.ctx, (self.width(), self.height()), self.dpr, cfg)
        self.r.set_palette(args.palette)

        self.server = ControlServer(args.port)
        self.server.start()
        self.notify = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.mic = Mic() if args.mic else None
        self._img = None
        self._img_rect = QRectF()
        self._mask_key = None
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
        data, (x, y, w, h) = r.render()
        d = self.dpr
        img = QImage(data, w, h, w * 4, QImage.Format_RGBA8888_Premultiplied).mirrored(False, True)
        rect = QRectF(x / d, y / d, w / d, h / d)
        ca = r.caption_alpha()
        if ca > 0.003:
            self._draw_caption(img, rect, ca)
        self._img, self._img_rect = img, rect
        self.update(rect.toAlignedRect())
        self._update_mask()

    def _draw_caption(self, img: QImage, rect: QRectF, alpha: float):
        cx, cy, w, h = self.r.caption_rect()
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.scale(img.width() / rect.width(), img.height() / rect.height())
        p.translate(-rect.x(), -rect.y())
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
        p.end()

    def _update_mask(self):
        (x, y, w, h), is_ball = self.r.hit_rect()
        key = (round(x), round(y), round(w), round(h), is_ball)
        if key == self._mask_key:
            return
        self._mask_key = key
        rect = QRect(key[0], key[1], key[2], key[3])
        self.setMask(QRegion(rect, QRegion.Ellipse) if is_ball else QRegion(rect))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(e.rect(), Qt.transparent)
        if self._img is not None:
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setClipRect(e.rect())
            p.drawImage(self._img_rect, self._img)
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
