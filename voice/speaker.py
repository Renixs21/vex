"""The voice: turns replies into speech, one sentence at a time, and pushes levels to the visual while it plays.

  * Pipelined: sentence N+1 is synthesised while sentence N plays, so speech starts after the first sentence is ready.
  * Long replies (over [voice] spoken_max_chars): the first couple of sentences are spoken, then "The rest is on screen. Say 'read
    the rest' to hear it." The remainder is kept; speak_rest() reads it all (no summary, no hint).
  * stop() works from any thread at any moment (barge-in): playback and any queued synthesis stop at once.
  * After speaking, a short pause so the microphone doesn't hear the tail of Vex's own voice.
Implements the ui.speaker.Speaker protocol, so it drops in where SimulatedSpeaker was.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Callable

from voice.timeline import timeline
from voice.tts import Audio, TTSChain, TTSUnavailable

log = logging.getLogger("vex.speaker")

HINT = 'The rest is on screen. Say "read the rest" to hear it.'
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])|\n+")


class SpeechUnavailable(TTSUnavailable):
    """Nothing could be spoken (every TTS provider failed); show the text instead."""


def split_sentences(text: str, min_chars: int = 25, max_chars: int = 240) -> list[str]:
    """Sentences for pipelined synthesis: tiny fragments are joined to the next one, huge ones are split at ; or ,."""
    parts = [p.strip() for p in _BOUNDARY.split(text.strip()) if p and p.strip()]
    merged: list[str] = []
    carry = ""
    for p in parts:
        p = f"{carry} {p}".strip() if carry else p
        if len(p) < min_chars:
            carry = p
        else:
            merged.append(p)
            carry = ""
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    out: list[str] = []
    for s in merged:
        while len(s) > max_chars:
            cut = max((s.rfind(sep, 0, max_chars) for sep in ("; ", ", ", " ")), key=lambda i: i)
            cut = cut if cut > min_chars else max_chars
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            out.append(s)
    return out


def plan_speech(text: str, max_chars: int = 280, max_sentences: int = 2) -> tuple[str, str]:
    """(what to speak now, what is left). Short replies are spoken whole; long ones as a short opening plus a hint."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text, ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    taken: list[str] = []
    for s in sentences:
        if len(taken) >= max_sentences or len(" ".join([*taken, s])) > max_chars:
            break
        taken.append(s)
    if not taken:                                                    # the first sentence alone is too long: cut at a word
        first = sentences[0]
        cut = first.rfind(" ", 0, max_chars)
        cut = cut if cut > 0 else max_chars
        spoken, rest = first[:cut].rstrip(",;: "), " ".join([first[cut:].strip(), *sentences[1:]]).strip()
        return f"{spoken}. {HINT}", rest
    return f"{' '.join(taken)} {HINT}", " ".join(sentences[len(taken):])


class VoiceSpeaker:
    def __init__(self, chain: TTSChain, player, max_chars: int = 280, max_sentences: int = 2, guard_s: float = 0.3,
                 sleep: Callable[[float], None] = time.sleep):
        self.chain = chain
        self.player = player
        self.max_chars = max_chars
        self.max_sentences = max_sentences
        self.guard_s = guard_s
        self._sleep = sleep
        self._cancel: threading.Event | None = None
        self._remaining = ""

    @property
    def has_remaining(self) -> bool:
        return bool(self._remaining)

    # ---- Speaker protocol ---------------------------------------------------------------------------------------------
    def speak(self, text: str, on_level: Callable[[float], None]) -> None:
        """Speak a reply (a summary if it's long). Blocks until done or stop()ped. Raises SpeechUnavailable if nothing played."""
        spoken, self._remaining = plan_speech(text, self.max_chars, self.max_sentences)
        self._play(split_sentences(spoken), on_level, first_word=True)

    def speak_rest(self, on_level: Callable[[float], None]) -> bool:
        """Speak what the last long reply left over, in full. False if there is nothing left."""
        rest, self._remaining = self._remaining, ""
        if not rest:
            return False
        self._play(split_sentences(rest), on_level)
        return True

    def say(self, text: str, on_level: Callable[[float], None]) -> None:
        """Speak exactly this text (confirmation questions, notices): no summarising, doesn't touch the remainder."""
        self._play(split_sentences(text), on_level)

    def warm(self) -> None:
        """Open the speech connection ahead of the reply (start of every turn); never blocks or raises."""
        fn = getattr(self.chain, "warm", None)
        if fn is not None:
            fn()

    def stop(self) -> None:
        cancel = self._cancel
        if cancel is not None:
            cancel.set()
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001
            log.debug("player stop failed", exc_info=True)

    # ---- pipeline -------------------------------------------------------------------------------------------------------------
    def _play(self, chunks: list[str], on_level: Callable[[float], None], first_word: bool = False) -> None:
        """`first_word`: this is a reply (not a confirmation question), so its first sound ends the latency stopwatch."""
        if not chunks:
            return
        cancel, done = threading.Event(), threading.Event()       # cancel: stop() was called; done: we're leaving, free the producer
        self._cancel = cancel
        q: "queue.Queue[object]" = queue.Queue(maxsize=2)

        def put(item) -> bool:
            while not (cancel.is_set() or done.is_set()):
                try:
                    q.put(item, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def producer() -> None:
            for c in chunks:
                if cancel.is_set() or done.is_set():
                    return
                try:
                    audio = self.chain.synthesize(c)
                except Exception as e:  # noqa: BLE001
                    put(e)
                    return
                if not put(audio):
                    return
            put(None)

        t = threading.Thread(target=producer, name="tts-producer", daemon=True)
        t.start()
        log.info("speech started (%d sentence(s))", len(chunks))
        played = 0
        try:
            while not cancel.is_set():
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, Exception):
                    if played == 0:
                        raise SpeechUnavailable(str(item)) from item
                    log.warning("speech stopped part-way: %s", item)           # the caption still has the full text
                    break
                assert isinstance(item, Audio)
                if first_word and played == 0:
                    timeline.mark("first_audio")                                  # the first sound starts now
                    timeline.set(tts_provider=getattr(self.chain, "last_provider", None))
                self.player.play(item.samples, item.rate, on_level)
                played += 1
        finally:
            done.set()
            t.join(timeout=2)
            on_level(0.0)
            log.info("speech ended (%d sentence(s) played%s, via %s)", played, ", stopped" if cancel.is_set() else "",
                     getattr(self.chain, "last_provider", None) or "?")
            if not cancel.is_set():
                self._sleep(self.guard_s)                                     # let the room go quiet before the mic opens
