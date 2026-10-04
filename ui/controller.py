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
FOLLOWUP = "followup"           # the window after a reply: listening without the wake word (the orb shows "listening")

_READ_REST = re.compile(
    r"^(?:please\s+)?(?:(?:read|say|speak)\s+(?:me\s+)?(?:the\s+)?(?:rest|remaining|remainder|everything|it\s+all|all\s+of\s+it|"
    r"the\s+whole\s+(?:thing|reply|answer)|the\s+rest\s+of\s+(?:it|that|the\s+(?:reply|answer)))|read\s+it(?:\s+(?:out|aloud|out\s+loud))?|"
    r"continue\s+reading)(?:\s+(?:out|aloud|out\s+loud|to\s+me))?$")


from voice.wakeword import strip_wake_phrase  # noqa: E402

_SLEEP = re.compile(r"^(?:please\s+)?(?:go\s+(?:back\s+)?to\s+sleep|go\s+to\s+the\s+edge|sleep(?:\s+now|\s+mode)?|"
                    r"(?:you\s+can\s+)?(?:dock|hide|minimi[sz]e)(?:\s+(?:now|yourself))?|that(?:'|\s)?s\s+all|"
                    r"good\s*night(?:\s+vex)?)(?:\s+(?:vex|please))?$")


_LEAD = re.compile(r"^(?:stop|wait|hold\s+on|no(?!\s+thanks?\b)|nope|nah|okay|ok|alright|all\s+right|right|so|well|now|please|just|and|then|hey|vex|hey\s+vex|"
                   r"you\s+(?:can|may|could)|can\s+you|could\s+you|will\s+you|i\s+said)\s+")
_TRAIL = re.compile(r"\s+(?:now|please|vex|then|for\s+now|for\s+today|thanks|thank\s+you|okay|ok)$")
# The fast path: short, clear sleep commands only. Everything else goes to the brain, which has the go_to_sleep tool.
_DONE = re.compile(r"^(?:go\s+(?:back\s+)?to\s+sleep|sleep(?:\s+mode)?|go\s+(?:back\s+)?to\s+(?:your\s+|the\s+)?(?:dock|doc|edge|pill)|"
                   r"(?:dock|hide|minimi[sz]e)(?:\s+yourself)?|good\s*bye|bye(?:\s+bye)?|good\s*night|"
                   r"that(?:'|\s)?s\s+(?:all|it)|that\s+will\s+be\s+all|stop\s+listening)$")
_QUESTION = re.compile(r"^(?:how|what|when|why|where|who|which|whose|should|shall|do|does|did|is|are|am|was|were|have|has|"
                       r"can\s+i|could\s+i|may\s+i|will\s+i|would\s+i)\b")
_POLITE = re.compile(r"^(?:can|could|will|would)\s+you\b")
FAST_MAX_WORDS = 5


def _word_forms(text: str) -> list[str]:
    """The words, then the same with filler peeled off step by step: "no, okay, you can go to sleep now please" -> ... -> "go to sleep"."""
    t = " ".join(re.sub(r"[^a-z\s']", " ", (text or "").lower().replace("’", "'")).split())
    forms = [t]
    for _ in range(12):
        t2 = _TRAIL.sub("", _LEAD.sub("", t))
        if t2 == t:
            break
        t = t2
        forms.append(t)
    return forms


def _normal_words(text: str) -> str:
    return _word_forms(text)[-1]


def _is_question(text: str) -> bool:
    """'how do I go to sleep', 'what time should I sleep', 'should I sleep now?': about sleep, not a command to Vex."""
    forms = _word_forms(text)
    first = next((f for f in forms if not re.match(r"^(?:okay|ok|hey|vex|hey\s+vex|so|well|and|now)\b", f)), forms[-1])
    if _QUESTION.match(first):
        return True
    return (text or "").strip().endswith("?") and not _POLITE.match(first)


def done_intent(text: str) -> str | None:
    """The fast path: 'sleep' for a short, clear command (go to sleep / your dock|doc / that's all / good night / goodbye / stop
    listening, with optional 'okay Vex', 'you can', 'now'...). None for anything else, and never for a question about sleep: those
    go to the brain, which can call the go_to_sleep tool for any other phrasing. Either way the result is the same: a short reply,
    the pill, wake word only, never a follow-up window."""
    if _is_question(text):
        return None
    for t in _word_forms(text):
        if t and len(t.split()) <= FAST_MAX_WORDS and _DONE.match(t):
            return "sleep"
    return None


def _sleep_tool():
    """tools.assistant (the go_to_sleep flag), or None where tools aren't loaded."""
    try:
        from tools import assistant

        return assistant
    except Exception:  # noqa: BLE001
        return None


def is_sleep(text: str) -> bool:
    """True for 'sleep', 'go to sleep', 'you can go to sleep now', 'dock', 'that's all', 'goodbye', 'stop listening', ...:
    the orb shrinks back into the edge pill."""
    return done_intent(text) is not None


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
        self.on_mute_change: list[Callable[[], None]] = []             # after the microphone is muted/unmuted (read controller.mic_muted)
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
        # follow-up listening: after a spoken reply, listen for this long without the wake word (the app sets these from [voice])
        self.followup_enabled = True
        self.followup_seconds = 8.0
        self.followup_min_speech_ms = 250                        # voiced audio needed before the window counts it as speech
        self.followup_delay_s = 0.3                              # after playback has fully ended (Vex's own tail / room echo)
        self._confirm_asked = False
        self._last_outcome = ""
        self._sleep = time.sleep
        # a bare "Hey Vex" (nothing after it): Vex says the greeting and listens for the command (the follow-up window)
        self.wake_greeting = "What can I do for you?"
        self.wake_wait_s = 3.0                                    # after the wake word, how long to wait for a command before greeting
        self._source = ""

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
            if self.state == FOLLOWUP:                               # the window is open: end it and run this trigger as a normal turn
                log.info("follow-up window interrupted by %s", source)
                self._pending = ("typed", None) if typed else None
                self._barge_in = True
                self.cancel_capture()
                return "barge_in"
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
            if self.state in (SPEAKING, FOLLOWUP):              # stop talking / listening, then run this text as the next turn
                self._pending = ("text", text)
                self._barge_in = True
                if self.state == FOLLOWUP:
                    self.cancel_capture()
                else:
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
                if self.state == FOLLOWUP:                            # don't make a reminder wait for the window: close it now
                    self.cancel_capture()
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
        self._source = source
        self._thread = threading.Thread(target=self._worker, args=(mode, text), name="vex-turn", daemon=True)
        self._thread.start()
        return "started"

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        if muted and self.state in (LISTENING, FOLLOWUP):
            self.cancel_capture()
        self._run_hooks(self.on_mute_change, "mute change")           # the app stops/starts the wake word listener, the tray icon follows

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
    def note_confirmation(self) -> None:
        """A confirmation question was asked during this turn (its own input flow): no follow-up window after it."""
        self._confirm_asked = True

    def _set_state(self, state: str) -> None:
        self.state = state
        self.frontend.set_state(LISTENING if state == FOLLOWUP else state)    # the visuals know four states; the window looks like listening
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
        warm = getattr(self.speaker, "warm", None)
        if warm is not None:
            warm()                                                # open the speech connection now, so the first sentence skips the handshake
        self._run_hooks(self.on_interaction_start, "start")
        try:
            while True:
                self._barge_in = False
                self._turn(mode, text)
                if self._shutdown:
                    break
                if self._barge_in:
                    mode, text = self._pending or ("listen", None)    # after a barge-in: the queued turn, else just listen
                    self._pending = None
                    timeline.begin("barge_in")
                    continue
                if not self._should_follow_up(mode):
                    break
                mode, text = "followup", None                     # keep listening a little: no wake word needed for the next thing
                timeline.begin("followup")
        finally:
            self._active_capture = None
            self._set_state(IDLE)
            if self._sleep_after:                                # "sleep": the orb shrinks back into the edge pill
                self._sleep_after = False
                self.frontend.show_caption("")
                self.frontend.hide()
                log.info("parked: orb -> pill, wake word only")
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

    def _should_follow_up(self, mode: str) -> bool:
        """Open the follow-up window after this turn? Only after a spoken answer to something you SAID, never after silence, an error,
        a confirmation question, a typed command, a reminder, 'sleep', or while muted; and only if the capture can do it."""
        why = None
        if not self.followup_enabled or self.followup_seconds <= 0:
            why = "off"
        elif mode not in ("listen", "followup"):
            why = f"{mode} turn"
        elif self._last_outcome not in ("spoken", "read_rest", "greeted"):
            why = self._last_outcome or "no outcome"
        elif self._confirm_asked:
            why = "a confirmation was asked"
        elif self.mic_muted or self._sleep_after or self._announcements:
            why = "muted" if self.mic_muted else ("sleep" if self._sleep_after else "an announcement is waiting")
        elif not hasattr(self.capture, "listen_followup"):
            why = "capture can't"
        if why is not None:
            log.debug("no follow-up window (%s)", why)
            return False
        return True

    def _followup_capture(self) -> str | None:
        """The window itself: wait for the speaker's tail to die away, show 'still listening', then listen without the wake word."""
        self._set_state(FOLLOWUP)
        self.frontend.show_caption("Still listening…")
        extra = max(0.0, self.followup_delay_s - float(getattr(self.speaker, "guard_s", 0.0) or 0.0))
        if extra > 0:
            self._sleep(extra)                                       # the speaker already waited guard_s after the last sound
        if self._barge_in or self._shutdown:
            return None
        log.info("follow-up window open for %.0f s", self.followup_seconds)
        self._active_capture = self.capture
        text = self.capture.listen_followup(self.followup_seconds, self.followup_min_speech_ms)
        if not text:
            log.info("follow-up window closed%s", " (interrupted)" if self._barge_in else " in silence: back to the wake word")
        return text

    def _turn(self, mode: str = "listen", given: str | None = None) -> None:
        self._confirm_asked = False
        if mode == "announce":
            self._announce_turn(given or "")
            self._last_outcome = "announce"
            return
        outcome = "followup_expired" if mode == "followup" else "no_speech"
        try:
            if mode == "followup":
                text = self._followup_capture()
                if not text and self._barge_in:
                    outcome = "followup_cancelled"
                if text:
                    raw = text
                    text = strip_wake_phrase(text)                       # "Hey Vex, and the weather?" works too
                    if text != raw.strip():
                        log.info("wake phrase in transcript: %r -> %r", raw, text)
                    if not text.strip() and self.wake_greeting and not self._barge_in:
                        outcome = self._greet()                          # a bare "Hey Vex" in the window: greet, listen again
                        return
            elif mode == "text":
                text = given
            else:
                self._set_state(LISTENING)
                self._active_capture = self.typed_capture if mode == "typed" else self.capture
                after_wake = mode == "listen" and self._source == "wakeword" and hasattr(self._active_capture, "after_wake")
                text = self._active_capture.after_wake(self.wake_wait_s) if after_wake else self._active_capture()
                if text and mode != "typed":
                    raw = text
                    text = strip_wake_phrase(text)                       # "Hey Vex, open notepad" -> "open notepad"
                    if text != raw.strip():
                        log.info("wake phrase in transcript: %r -> %r", raw, text)
                if (self._source == "wakeword" and mode == "listen" and not (text or "").strip() and not self._shutdown
                        and not self._barge_in and not self.mic_muted and self.wake_greeting):
                    outcome = self._greet()                          # a bare "Hey Vex": answer it out loud, then listen
                    return
            if not text or not text.strip() or self._shutdown:
                self.frontend.show_caption("")
                return
            intent = done_intent(text)
            if intent is not None:                                   # sleep / goodbye / that's all / thanks: short reply, then the pill
                outcome = "followup_end" if mode == "followup" else "sleep"
                log.info("done intent (%s) from %r: short reply, then wake-word only", intent, text)
                self._sleep_after = True
                self.frontend.show_caption("")
                self._set_state(SPEAKING)
                self.speaker.speak("Okay.", self.frontend.set_level)
                return
            text = text.strip()
            timeline.mark("text_ready")
            self.frontend.show_caption(f"“{text}”")
            if self._read_rest(text):
                outcome = "read_rest"
                return
            self._set_state(THINKING)
            sleep_tool = _sleep_tool()
            if sleep_tool is not None:
                sleep_tool.clear_sleep_request()
            reply = self.agent.run(text)
            timeline.mark("brain_end")
            if sleep_tool is not None and sleep_tool.take_sleep_request() and not self._barge_in:
                outcome = "sleep"                                    # the brain called go_to_sleep: same ending as the fast path
                log.info("go_to_sleep called by the brain for %r: short reply, then wake-word only", text)
                self._sleep_after = True
                short = reply.strip() if reply and len(reply.strip()) <= 40 else "Okay."
                self.frontend.show_caption("")
                self._set_state(SPEAKING)
                self.speaker.speak(short, self.frontend.set_level)
                return
            outcome = "interrupted"
            if self._barge_in:                                   # interrupted while thinking: skip speaking
                log.info("tts skipped: interrupted while thinking")
                return
            if not (reply or "").strip():
                log.info("tts skipped: the reply was empty")
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
            self._last_outcome = outcome
            timeline.set(followup=mode == "followup")
            timeline.finish(outcome)

    def _greet(self) -> str:
        """The spoken answer to a bare wake word. Returns the turn outcome ('greeted' opens the follow-up window for the command)."""
        log.info("wake word with no command: greeting")
        self._set_state(SPEAKING)
        self.frontend.show_caption(self.wake_greeting)
        self.speaker.speak(self.wake_greeting, self.frontend.set_level)
        return "interrupted" if self._barge_in else "greeted"

    def _read_rest(self, text: str) -> bool:
        """'Read the rest': speak what the last long reply left over, straight from the speaker, without asking the LLM."""
        if not is_read_rest(text) or not hasattr(self.speaker, "speak_rest"):
            return False
        self._set_state(SPEAKING)
        if not self.speaker.speak_rest(self.frontend.set_level):
            self.frontend.show_caption("There's nothing more to read.")
            self.speaker.speak("There's nothing more to read.", self.frontend.set_level)
        return True
