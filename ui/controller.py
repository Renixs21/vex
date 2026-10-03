"""The controller: one place that runs a voice turn, whoever triggers it.

    start_listening()  <- hotkey, tray "Listen now", clicking the visual (and later the wake word)
        idle -> listening -> (capture) -> thinking -> (agent) -> speaking -> idle

Pressing the trigger while Vex is speaking is barge-in: speech stops and a new listening turn starts. Triggers while
listening or thinking are ignored. The whole interaction (including barge-in restarts) is bracketed by
on_interaction_start / on_interaction_end hooks, which always run (try/finally): that is where a wake-word listener will be
paused and resumed. No Qt in here, so it is easy to test and to drive from any frontend.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

from ui.frontend import VisualFrontend
from ui.speaker import Speaker

log = logging.getLogger("vex.controller")

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"


class Capture(Protocol):
    def __call__(self) -> str | None:
        """Get the user's next command as text (microphone + STT, or a typed prompt). None = nothing / cancelled."""

    def cancel(self) -> None: ...


class Agent(Protocol):
    def run(self, text: str) -> str: ...


class VexController:
    def __init__(self, agent: Agent, frontend: VisualFrontend, speaker: Speaker, capture: Capture):
        self.agent = agent
        self.frontend = frontend
        self.speaker = speaker
        self.capture = capture
        self.state = IDLE
        self.mic_muted = False
        self.on_interaction_start: list[Callable[[], None]] = []
        self.on_interaction_end: list[Callable[[], None]] = []
        self.listeners: list[Callable[[str], None]] = []       # called with the new state on every change
        self.on_wakeword_toggle: list[Callable[[bool], None]] = []   # the tray's Wake word switch (listener comes later)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._barge_in = False
        self._shutdown = False

    # ---- triggers ----------------------------------------------------------------------------------------------------
    def start_listening(self, source: str = "hotkey") -> str:
        """The single entry point for hotkey, tray, click and wake word.

        Returns what happened: "started", "barge_in", "busy" or "muted".
        """
        with self._lock:
            if self._shutdown:
                return "busy"
            if self.mic_muted:
                self.frontend.show_caption("The microphone is muted.")
                return "muted"
            if self.state == SPEAKING:
                log.info("barge-in from %s", source)
                self._barge_in = True
                self.speaker.stop()
                return "barge_in"
            if self.state != IDLE or (self._thread is not None and self._thread.is_alive()):
                log.info("ignoring %s trigger: busy (%s)", source, self.state)
                return "busy"
            self._thread = threading.Thread(target=self._worker, name="vex-turn", daemon=True)
            self._thread.start()
            return "started"

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        if muted and self.state == LISTENING:
            self.cancel_capture()

    def cancel_capture(self) -> None:
        try:
            self.capture.cancel()
        except Exception:  # noqa: BLE001
            log.debug("capture cancel failed", exc_info=True)

    def shutdown(self) -> None:
        """Stop everything; used when quitting."""
        with self._lock:
            self._shutdown = True
        self.speaker.stop()
        self.cancel_capture()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the current interaction has finished (for tests and clean shutdown)."""
        t = self._thread
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    # ---- the turn ----------------------------------------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.state = state
        self.frontend.set_state(state)
        for cb in list(self.listeners):
            try:
                cb(state)
            except Exception:  # noqa: BLE001
                log.exception("state listener failed")

    def _run_hooks(self, hooks: list[Callable[[], None]], label: str) -> None:
        for h in list(hooks):
            try:
                h()
            except Exception:  # noqa: BLE001 - a broken hook must not wedge the interaction
                log.exception("%s hook failed", label)

    def _worker(self) -> None:
        self._run_hooks(self.on_interaction_start, "start")
        try:
            while True:
                self._barge_in = False
                self._turn()
                if not self._barge_in or self._shutdown:
                    break
        finally:
            self._set_state(IDLE)
            self._run_hooks(self.on_interaction_end, "end")

    def _turn(self) -> None:
        try:
            self._set_state(LISTENING)
            text = self.capture()
            if not text or not text.strip() or self._shutdown:
                self.frontend.show_caption("")
                return
            text = text.strip()
            self._set_state(THINKING)
            self.frontend.show_caption(f"“{text}”")
            reply = self.agent.run(text)
            if self._barge_in:                                   # interrupted while thinking: skip speaking
                return
            self._set_state(SPEAKING)
            self.frontend.show_caption(reply)
            self.speaker.speak(reply, self.frontend.set_level)
        except Exception:  # noqa: BLE001 - one bad turn must not kill the app
            log.exception("turn failed")
            self.frontend.show_caption("Something went wrong. Try again.")
        finally:
            self.frontend.set_level(0.0)
