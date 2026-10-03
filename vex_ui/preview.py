"""
Render preview videos off-screen (no window needed). Needs ffmpeg on PATH.
    python preview.py              -> vex_preview_desktop.mp4  (overlay on a mock 16:9 desktop)
    python preview.py --window     -> vex_preview_window.mp4   (widescreen window mode)
    python preview.py --stills     -> a few PNG frames instead of video
"""
import subprocess
import sys

import moderngl
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from vex_ui.renderer import VexRenderer

FPS = 30
DW, DH = 1280, 720          # desktop / video size
OW, OH = 420, 340           # overlay window size (the real app default)
TASKBAR = 44

SCRIPT = [
    (0.0, ("wake",)),
    (0.0, ("caption", "")),
    (1.2, ("caption", "What can I do for you today?")),
    (5.0, ("state", "listening")), (5.0, ("caption", "Listening…")),
    (8.0, ("state", "thinking")), (8.0, ("caption", "Thinking…")),
    (10.5, ("state", "speaking")), (10.5, ("caption", "Opening YouTube and playing lo-fi beats.")),
    (14.5, ("state", "idle")), (14.5, ("caption", "")),
    (15.5, ("state", "sleep")),
]
DURATION = 17.5


def mock_desktop():
    """A neutral wallpaper with a generic app window and taskbar (just to show the overlay in context)."""
    y, x = np.mgrid[0:DH, 0:DW].astype("f4")
    a = np.stack([0.20 + 0.18 * x / DW, 0.24 + 0.10 * y / DH, 0.30 + 0.16 * (1 - y / DH)], -1)
    blob = np.exp(-(((x - DW * 0.75) / 380) ** 2 + ((y - DH * 0.3) / 260) ** 2))[..., None]
    a = a * (1 - blob * 0.5) + np.array([0.55, 0.45, 0.62]) * blob * 0.5
    img = Image.fromarray((np.clip(a, 0, 1) * 255).astype("u1"), "RGB").filter(ImageFilter.GaussianBlur(2))
    d = ImageDraw.Draw(img)
    # a generic editor window on the left
    d.rounded_rectangle((70, 60, 600, 470), 10, fill=(32, 34, 40))
    d.rounded_rectangle((70, 60, 600, 92), 10, fill=(44, 46, 54))
    for i in range(12):
        w = 120 + (i * 53) % 300
        d.rounded_rectangle((96, 112 + i * 27, 96 + w, 122 + i * 27), 4,
                            fill=[(120, 160, 255), (190, 190, 200), (140, 210, 160)][i % 3])
    # taskbar
    d.rectangle((0, DH - TASKBAR, DW, DH), fill=(26, 28, 34))
    for i in range(6):
        cx = DW // 2 - 125 + i * 50
        d.rounded_rectangle((cx - 14, DH - TASKBAR + 8, cx + 14, DH - 8), 6, fill=(70 + i * 12, 90, 140 + i * 10))
    d.text((DW - 70, DH - 30), "22:04", fill=(220, 220, 225))
    return np.asarray(img).astype("f4") / 255.0


def run(mode, stills):
    W, H = (OW, OH) if mode == "overlay" else (DW, DH)
    ctx = moderngl.create_standalone_context(backend="egl") if sys.platform.startswith("linux") \
        else moderngl.create_standalone_context()
    fbo = ctx.simple_framebuffer((W, H), components=4)
    fbo.use()
    r = VexRenderer(ctx, (W, H), mode=mode, flip=True)
    desk = mock_desktop() if mode == "overlay" else None
    ox, oy = (DW - OW) // 2, DH - TASKBAR - OH

    out = f"vex_preview_{'desktop' if mode == 'overlay' else 'window'}.mp4"
    proc = None if stills else subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{DW}x{DH}",
         "-r", str(FPS), "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", out],
        stdin=subprocess.PIPE)
    queue, dt = list(SCRIPT), 1.0 / FPS
    marks = {int(s * FPS) for s in (0.7, 1.5, 2.1, 4.5, 6.5, 9.2, 12.0, 16.0)}
    for i in range(int(DURATION * FPS)):
        t = i * dt
        while queue and queue[0][0] <= t:
            _, act = queue.pop(0)
            {"wake": lambda: r.wake(),
             "state": lambda: r.set_state(act[1]),
             "caption": lambda: r.set_caption(act[1])}[act[0]]()
        fbo.clear(0, 0, 0, 0)
        r.render(t, dt)
        px = np.frombuffer(fbo.read(components=4), "u1").reshape(H, W, 4).astype("f4") / 255.0
        if mode == "overlay":
            frame = desk.copy()
            region = frame[oy:oy + OH, ox:ox + OW]
            frame[oy:oy + OH, ox:ox + OW] = px[..., :3] + region * (1 - px[..., 3:4])   # premultiplied over
        else:
            frame = px[..., :3]
        rgb = (np.clip(frame, 0, 1) * 255).astype("u1")
        if proc:
            proc.stdin.write(rgb.tobytes())
        if stills and i in marks:
            Image.fromarray(rgb).save(f"still_{mode}_{t:04.1f}.png")
    if proc:
        proc.stdin.close(); proc.wait()
        print("wrote", out)


if __name__ == "__main__":
    run("window" if "--window" in sys.argv else "overlay", "--stills" in sys.argv)
