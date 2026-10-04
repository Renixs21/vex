"""
Tiny client for Vex's backend to drive the visual.

    from vex_ui.client import VexUI
    ui = VexUI()
    ui.wake()                         # after "Hey Vex" is detected
    ui.listening("Listening…")
    ui.thinking("Thinking…")
    ui.speak_file("reply.mp3", "Opening YouTube…")   # plays gTTS audio + animates
    ui.idle("What can I do for you today?")
    ui.sleep()                        # fade away until the next "Hey Vex"
"""
from __future__ import annotations

import json
import socket
import threading
import time

PORT = 47474


class VexUI:
    def __init__(self, host: str = "127.0.0.1", port: int = PORT):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, **msg):
        self.sock.sendto(json.dumps(msg).encode("utf-8"), self.addr)

    # ---- states ----
    def wake(self, caption: str | None = "What can I do for you today?"):
        self.send(wake=True, state="idle", **({"caption": caption} if caption is not None else {}))

    def idle(self, caption: str | None = None):
        self.send(state="idle", **({"caption": caption} if caption is not None else {}))

    def listening(self, caption: str | None = None):
        self.send(state="listening", **({"caption": caption} if caption is not None else {}))

    def thinking(self, caption: str | None = None):
        self.send(state="thinking", **({"caption": caption} if caption is not None else {}))

    def speaking(self, caption: str | None = None):
        self.send(state="speaking", **({"caption": caption} if caption is not None else {}))

    def sleep(self):
        """Fade the orb away (overlay becomes fully transparent and click-through)."""
        self.send(state="sleep")

    def palette(self, name: str):
        """Change colour: 'blue', 'default' (grey), 'violet' or 'emerald'."""
        self.send(palette=name)

    def level(self, value: float):
        """Feed a real audio level 0..1 (otherwise speaking uses a natural fake envelope)."""
        self.send(level=float(value))

    def caption(self, text: str):
        self.send(caption=text)

    def quit(self):
        self.send(quit=True)

    # ---- convenience: play a gTTS mp3 and animate in sync ----
    def speak_file(self, path: str, caption: str | None = None, then_idle: str | None = ""):
        """
        Plays an audio file (e.g. saved from gTTS) and puts the orb in 'speaking' for its duration.
        If pydub + ffmpeg are installed, the real loudness envelope drives the orb; otherwise a
        natural-looking fake envelope is used. Blocks until playback finishes.
        """
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        envelope = None
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_file(path)
            step = 30  # ms
            peak = max(1, seg.max)
            envelope = [min(1.0, (seg[i:i + step].rms / peak) * 2.2) for i in range(0, len(seg), step)]
        except Exception:
            pass

        self.speaking(caption)
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        t0 = time.perf_counter()
        while pygame.mixer.music.get_busy():
            if envelope:
                i = int((time.perf_counter() - t0) * 1000 / 30)
                if i < len(envelope):
                    self.level(envelope[i])
            time.sleep(0.03)
        self.level(0.0)
        if then_idle is not None:
            self.idle(then_idle or None)


def listen_events(callback, port: int = 47475):
    """
    Get events back from the dock UI (runs in a background thread).
    Currently: {"event": "wake_clicked"} when the user wakes Vex by clicking the pill or tray icon,
    so the backend can start listening just like after "Hey Vex".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))

    def loop():
        while True:
            data, _ = sock.recvfrom(65536)
            try:
                callback(json.loads(data.decode("utf-8")))
            except Exception:
                pass

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return th


if __name__ == "__main__":
    # quick demo while the window is running:  python -m vex_ui.client
    ui = VexUI()
    ui.wake(); time.sleep(4)
    ui.listening("Listening…"); time.sleep(3)
    ui.thinking("Thinking…"); time.sleep(2.5)
    ui.speaking("Sure — playing lo-fi beats on YouTube."); time.sleep(4)
    ui.idle("What can I do for you today?"); time.sleep(3)
    ui.sleep()
