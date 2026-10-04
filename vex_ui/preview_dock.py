"""
Records the REAL dock window code (offscreen, stepped by hand) over a mock desktop.
    python preview_dock.py [out.mp4] [--speed 1.0]
"""
import os, sys, subprocess
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from vex_ui.app import parse_args
from vex_ui.dock_app import DockWindow
from preview import mock_desktop, DW, DH

out = next((a for a in sys.argv[1:] if a.endswith(".mp4")), "vex_dock_preview.mp4")
speed = sys.argv[sys.argv.index("--speed") + 1] if "--speed" in sys.argv else "1.0"
app = QApplication([])
args = parse_args(["--port", "47998", "--notify-port", "47997", "--speed", speed])
win = DockWindow(args)
win.timer.stop()
win.setGeometry(0, 0, DW, DH)                         # fixed test size
win.r.W, win.r.H, win.r.pw, win.r.ph = DW, DH, DW, DH
win.r.fbo = win.ctx.simple_framebuffer((DW, DH), components=4)
win.dpr = win.r.dpr = 1.0
win.show(); app.processEvents()

FPS, DUR = 30, 14.0
EV = [(0, "Parked on the edge"), (1.5, '"Hey Vex"'), (3.2, "Listening"), (5.2, "Thinking"),
      (7.0, "Speaking"), (9.5, '"Sleep"'), (11.5, "Parked on the edge")]
ACT = {1.5: lambda: (win.do_wake(), win.r.set_state("listening"), win.r.set_caption("Listening…")),
       5.2: lambda: (win.r.set_state("thinking"), win.r.set_caption("Thinking…")),
       7.0: lambda: (win.r.set_state("speaking"), win.r.set_caption("Opening YouTube and playing lo-fi beats.")),
       9.5: lambda: (win.r.set_state("sleep"), win.r.set_caption(""))}
desk = mock_desktop()
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{DW}x{DH}",
                         "-r", str(FPS), "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "17", out],
                        stdin=subprocess.PIPE)
dt, done = 1 / FPS, set()
masks = []
for i in range(int(DUR * FPS)):
    t = i * dt
    for k, fn in ACT.items():
        if t >= k and k not in done:
            done.add(k); fn()
    win.step(t, dt)
    img = win.grab().toImage().convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    px = np.frombuffer(img.constBits(), "u1").reshape(DH, DW, 4).astype("f4") / 255
    fr = px[..., :3] + desk * (1 - px[..., 3:4])
    pil = Image.fromarray((np.clip(fr, 0, 1) * 255).astype("u1"))
    label = [l for tt, l in EV if tt <= t][-1]
    d = ImageDraw.Draw(pil); w = d.textlength(label, font=font)
    d.rounded_rectangle((20, 18, 20 + w + 28, 56), 12, fill=(14, 16, 24)); d.text((34, 25), label, font=font, fill=(235, 240, 255))
    if round(t, 2) in (0.0, 1.5, 1.8, 2.1, 2.4, 2.9, 4.5, 6.0, 8.0, 10.0, 10.5, 11.0, 12.5):
        pil.save(f"dkey_{t:05.2f}.png")
    if i % 15 == 0: masks.append((round(t, 1), win._mask_key))
    proc.stdin.write(pil.tobytes())
proc.stdin.close(); proc.wait()
print("wrote", out); print("hit-mask samples:", masks[:8])
