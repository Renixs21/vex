"""The controller: one place that runs a voice turn, whoever triggers it.

    start_listening()  <- hotkey, tray "Listen now", clicking the visual (and later the wake word)
    start_listening(typed=True) / submit_text(text)  <- the tray's "Type a command"
        idle -> listening -> (capture) -> thinking -> (agent) -> speaking -> idle

Pressing the trigger while Vex is speaking is barge-in: speech stops and a new listening turn starts. Triggers while
listening or thinking are ignored. The whole interaction (including barge-in restarts) is bracketed by
on_interaction_start / on_interaction_end hooks, which always run (try/finally): that is where a wake-word listener will be
paused and resumed. "Read the rest" is handled here without calling the LLM. No Qt in here, so it is easy to test and to drive
from any frontend.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Protocol

from ui.frontend import VisualFrontend
from ui.speaker import Speaker
from voice.timeline import timeline

log = logging.getLogger("vex.controller")

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"

_READ_REST = re.compile(
    r"^(?:please\s+)?(?:(?:read|say|speak)\s+(?:me\s+)?(?:the\s+)?(?:rest|remaining|remainder|everything|it\s+all|all\s+of\s+it|"
    r"the\s+whole\s+(?:thing|reply|answer)|the\s+rest\s+of\s+(?:it|that|the\s+(?:reply|answer)))|read\s+it(?:\s+(?:out|aloud|out\s+loud))?|"
    r"continue\s+reading)(?:\s+(?:out|aloud|out\s+loud|to\s+me))?$")


from voice.wakeword import strip_wake_phrase  # noqa: E402

_SLEEP = re.compile(r"^(?:please\s+)?(?:go\s+(?:back\s+)?to\s+sleep|go\s+to\s+the\s+edge|sleep(?:\s+now|\s+mode)?|"
                    r"(?:you\s+can\s+)?(?:dock|hide|minimi[sz]e)(?:\s+(?:now|yourself))?|that(?:'|\s)?s\s+all|"
                    r"good\s*night(?:\s+vex)?)(?:\s+(?:vex|please))?$")


def is_sleep(text: str) -> bool:
    """True for 'sleep', 'go to sleep', 'dock', 'that's all', ... : the orb shrinks back into the edge pill."""
    t = re.sub(r"[^a-z\s']", " ", (text or "").lower())
    return bool(_SLEEP.match(" ".join(t.split())))


def is_read_rest(text: str) -> bool:
    """True for 'read the rest', 'say the rest', 'read it all', ... (case and punctuation ignored)."""
    t = re.sub(r"[^a-z\s]", " ", (text or "").lower())
    return bool(_READ_REST.match(" ".join(t.split())))


class Capture(Protocol):
    def __call__(self) -> str | None:
        """Get the user's next command as text (microphone + STT, or a typed prompt). None = nothing / cancelled."""

    def cancel(self) -> None: ...


class Agent(Protocol):
    def run(self, text: str) -> str: ...


class VexController:
    def __init__(self, agent: Agent, frontend: VisualFrontend, speaker: Speaker, capture: Capture,
                 typed_capture: Capture | None = None):
        self.agent = agent
        self.frontend = frontend
        self.speaker = speaker
        self.capture = capture
        self.typed_capture = typed_capture or capture
        self.state = IDLE
        self.mic_muted = False
        self.last_reply = ""
        self._sleep_after = False
        self.on_interaction_start: list[Callable[[], None]] = []
        self.on_interaction_end: list[Callable[[], None]] = []
        self.on_turn: list[Callable[[str, str], None]] = []       # (what you said, Vex's reply) after each answered turn
        self.listeners: list[Callable[[str], None]] = []       # called with the new state on every change
        self.on_wakeword_toggle: list[Callable[[bool], None]] = []   # the tray's Wake word switch (listener comes later)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._barge_in = False
        self._shutdown = False
        self._active_capture: Capture | None = None
        self._pending: tuple[str, str | None] | None = None      # the next turn queued by a barge-in (typed text / typed prompt)
        self.chime: Callable[[], None] | None = None             # played before Vex speaks on its own (a reminder)
        self._announcements: list[str] = []                      # things to say on Vex's own initiative, waiting for their turn

    # ---- triggers ----------------------------------------------------------------------------------------------------
    def start_listening(self, source: str = "hotkey", typed: bool = False) -> str:
        """The single entry point for hotkey, tray, click and wake word (typed=True: the tray's "Type a command").

        Returns what happened: "started", "barge_in", "busy" or "muted".
        """
        result = self._start_listening(source, typed)
        log.info("trigger from %s%s -> %s", source, " (typed)" if typed else "", result)
        return result

    def _start_listening(self, source: str, typed: bool) -> str:
        with self._lock:
            if self._shutdown:
                return "busy"
            if self.mic_muted and not typed:
                self.frontend.show_caption("The microphone is muted.")
                return "muted"
            if self.state == SPEAKING:
                log.info("barge-in from %s", source)
                self._pending = ("typed", None) if typed else None
                self._barge_in = True
                self.speaker.stop()
                return "barge_in"
            if self.state == IDLE and not (self._thread is not None and self._thread.is_alive()):
                timeline.begin(source)                                # the stopwatch starts at the trigger (before the turn thread runs)
            return self._spawn("typed" if typed else "listen", None, source)

    def submit_text(self, text: str, source: str = "typed") -> str:
        """Run a turn on text you already have (no microphone). Same states, same barge-in rules as listening."""
        with self._lock:
            if self._shutdown:
                return "busy"
            if self.state == SPEAKING:                           # stop talking, then run this text as the next turn
                self._pending = ("text", text)
                self._barge_in = True
                self.speaker.stop()
                return "barge_in"
            if self.state == IDLE and not (self._thread is not None and self._thread.is_alive()):
                timeline.begin(source)
            return self._spawn("text", text, source)

    def announce(self, text: str) -> str:
        """Say something on Vex's own initiative (a reminder fires): chime, caption, voice. If Vex is busy it waits its turn.

        Returns "started", "queued" or "busy" (shutting down). Never interrupts a conversation."""
        text = (text or "").strip()
        if not text:
            return "busy"
        with self._lock:
            if self._shutdown:
                return "busy"
            if self.state != IDLE or (self._thread is not None and self._thread.is_alive()):
                self._announcements.append(text)
                log.info("announcement queued: Vex is busy (%s)", self.state)
                return "queued"
            return self._spawn("announce", text, "announce")

    def _flush_announcements(self) -> None:
        """After an interaction ends: say what was queued, one at a time (each runs as its own interaction, so the wake word stays paused)."""
        with self._lock:
            if not self._announcements or self._shutdown:
                return
            text = self._announcements.pop(0)

        def later() -> None:
            for _ in range(40):
                time.sleep(0.4)                                   # lets the finishing interaction thread exit first
                with self._lock:
                    if self._shutdown:
                        return
                    if self.state == IDLE and not (self._thread is not None and self._thread.is_alive()):
                        self._spawn("announce", text, "announce")
                        return
            with self._lock:                                      # still busy after 16 s: keep it for the next interaction's end
                self._announcements.insert(0, text)

        threading.Thread(target=later, name="vex-announce", daemon=True).start()

    def _spawn(self, mode: str, text: str | None, source: str) -> str:
        if self.state != IDLE or (self._thread is not None and self._thread.is_alive()):
            log.info("ignoring %s trigger: busy (%s)", source, self.state)
            return "busy"
        self._thread = threading.Thread(target=self._worker, args=(mode, text), name="vex-turn", daemon=True)
        self._thread.start()
        return "started"

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        if muted and self.state == LISTENING:
            self.cancel_capture()

    def cancel_capture(self) -> None:
        for cap in {id(c): c for c in (self._active_capture, self.capture, self.typed_capture) if c is not None}.values():
            try:
                cap.cancel()
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

    def _worker(self, mode: str = "listen", text: str | None = None) -> None:
        self._run_hooks(self.on_interaction_start, "start")
        try:
            while True:
                self._barge_in = False
                self._turn(mode, text)
                if not self._barge_in or self._shutdown:
                    break
                mode, text = self._pending or ("listen", None)    # after a barge-in: the queued turn, else just listen
                self._pending = None
                timeline.begin("barge_in")
        finally:
            self._active_capture = None
            self._set_state(IDLE)
            if self._sleep_after:                                # "sleep": the orb shrinks back into the edge pill
                self._sleep_after = False
                self.frontend.show_caption("")
                self.frontend.hide()
            self._run_hooks(self.on_interaction_end, "end")
            self._flush_announcements()

    def _announce_turn(self, text: str) -> None:
        """A reminder (or other self-started message): chime, show the words, speak them. No listening, no LLM."""
        try:
            self._set_state(SPEAKING)
            self.frontend.show_caption(text)
            if self.chime is not None:
                try:
                    self.chime()
                except Exception:  # noqa: BLE001 - no chime is fine
                    log.debug("chime failed", exc_info=True)
            self.speaker.speak(text, self.frontend.set_level)
        except Exception:  # noqa: BLE001 - if it cannot be spoken the caption (and the toast) still carry it
            log.exception("announcement could not be spoken")
        finally:
            self.frontend.set_level(0.0)

    def _turn(self, mode: str = "listen", given: str | None = None) -> None:
        if mode == "announce":
            self._announce_turn(given or "")
            return
        outcome = "no_speech"
        try:
            if mode == "text":
                text = given
            else:
                self._set_state(LISTENING)
                self._active_capture = self.typed_capture if mode == "typed" else self.capture
                text = self._active_capture()
                if text and mode != "typed":
                    raw = text
                    text = strip_wake_phrase(text)                       # "Hey Vex, open notepad" -> "open notepad"
                    if text != raw.strip():
                        log.info("wake phrase in transcript: %r -> %r", raw, text)
            if not text or not text.strip() or self._shutdown:
                self.frontend.show_caption("")
                return
            text = text.strip()
            timeline.mark("text_ready")
            self.frontend.show_caption(f"“{text}”")
            if self._read_rest(text):
                outcome = "read_rest"
                return
            if is_sleep(text):
                self._sleep_after = True
                self._set_state(SPEAKING)
                self.speaker.speak("Okay.", self.frontend.set_level)
                return
            self._set_state(THINKING)
            reply = self.agent.run(text)
            timeline.mark("brain_end")
            outcome = "interrupted"
            if self._barge_in:                                   # interrupted while thinking: skip speaking
                return
            self.last_reply = reply
            for cb in list(self.on_turn):                        # e.g. the memory curator: cheap, must never break the turn
                try:
                    cb(text, reply)
                except Exception:  # noqa: BLE001
                    log.exception("turn listener failed")
            self._set_state(SPEAKING)
            self.frontend.show_caption(reply)
            self.speaker.speak(reply, self.frontend.set_level)
            outcome = "spoken"
        except Exception:  # noqa: BLE001 - one bad turn must not kill the app
            outcome = "error"
            log.exception("turn failed")
            self.frontend.show_caption("Something went wrong. Try again.")
        finally:
            self.frontend.set_level(0.0)
            timeline.finish(outcome)

    def _read_rest(self, text: str) -> bool:
        """'Read the rest': speak what the last long reply left over, straight from the speaker, without asking the LLM."""
        if not is_read_rest(text) or not hasattr(self.speaker, "speak_rest"):
            return False
        self._set_state(SPEAKING)
        if not self.speaker.speak_rest(self.frontend.set_level):
            self.frontend.show_caption("There's nothing more to read.")
            self.speaker.speak("There's nothing more to read.", self.frontend.set_level)
        return True
