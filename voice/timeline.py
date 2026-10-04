"""Per-turn latency timeline: where the time goes between "you stop talking" and "Vex says its first word".

One turn at a time (Vex never runs two). Each stage drops a named timestamp here; when the turn ends the summary goes to
`logs/latency.jsonl` (one JSON line per turn) and one readable line to vex.log. Nothing about WHAT was said is stored: only timings,
the provider names, counts and whether the sentence ended cleanly. `python tools_dev/latency_report.py` turns the file into medians.

Stages (seconds):
  trigger_to_record   hotkey / wake word / click -> the microphone is listening
  endpoint_wait       you stopped talking -> the recorder decided you were done (the silence it waits for)
  stt                 audio sent -> text back (upload + Whisper)
  brain               text in -> reply ready (language model calls and tools)
  tts_first_audio     reply ready -> the first sound plays (text-to-speech of the first sentence)
  total               you stopped talking -> the first spoken word  (what you feel)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("vex.timing")

WAKE_VALID_SECONDS = 3.0               # a wake detection older than this belongs to an earlier turn


class Timeline:
    def __init__(self, clock: Callable[[], float] = time.monotonic, path: Path | None = None):
        self._clock = clock
        self.path = path
        self._lock = threading.Lock()
        self._wake_t: float | None = None
        self._turn: dict | None = None

    def configure(self, path: Path | None) -> None:
        self.path = Path(path) if path else None

    # ---- marking -----------------------------------------------------------------------------------------------------------
    def note_wake(self, t: float | None = None) -> None:
        """The wake word was just heard (called before the turn is triggered)."""
        with self._lock:
            self._wake_t = self._clock() if t is None else t

    def begin(self, source: str, t: float | None = None) -> None:
        """A turn was triggered. Replaces any unfinished one (a barge-in, a cancelled capture)."""
        now = self._clock() if t is None else t
        with self._lock:
            wake = self._wake_t if self._wake_t is not None and 0 <= now - self._wake_t <= WAKE_VALID_SECONDS else None
            self._wake_t = None
            self._turn = {"source": source, "marks": {"trigger": now}, "llm": [], "tools": [], "data": {}}
            if wake is not None and source == "wakeword":
                self._turn["marks"]["wake"] = wake

    @property
    def active(self) -> bool:
        return self._turn is not None

    def mark(self, name: str, t: float | None = None, overwrite: bool = False) -> None:
        with self._lock:
            if self._turn is None:
                return
            marks = self._turn["marks"]
            if overwrite or name not in marks:
                marks[name] = self._clock() if t is None else t

    def set(self, **data) -> None:
        with self._lock:
            if self._turn is not None:
                self._turn["data"].update(data)

    def llm(self, provider: str, seconds: float, tools: int, prompt_tokens: int | None = None, ok: bool = True) -> None:
        with self._lock:
            if self._turn is not None:
                self._turn["llm"].append({"provider": provider, "seconds": round(seconds, 3), "tools": tools, "ok": ok,
                                          **({"prompt_tokens": prompt_tokens} if prompt_tokens else {})})

    def tool(self, name: str, seconds: float) -> None:
        with self._lock:
            if self._turn is not None:
                self._turn["tools"].append({"name": name, "seconds": round(seconds, 3)})

    # ---- finishing ---------------------------------------------------------------------------------------------------------
    def finish(self, outcome: str = "spoken") -> dict | None:
        """Close the turn: compute the stages, write the line. Returns the record (None if no turn was active)."""
        with self._lock:
            turn, self._turn = self._turn, None
        if turn is None:
            return None
        rec = summarize(turn, outcome)
        rec["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write(rec)
        return rec

    def _write(self, rec: dict) -> None:
        log.info("TURN TIMING %s", describe(rec))
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.debug("could not write the latency log", exc_info=True)


def _gap(marks: dict, a: str, b: str) -> float | None:
    if a in marks and b in marks and marks[b] >= marks[a]:
        return round(marks[b] - marks[a], 3)
    return None


def summarize(turn: dict, outcome: str) -> dict:
    m = turn["marks"]
    stages: dict[str, float] = {}
    for name, a, b in (("wake_to_trigger", "wake", "trigger"), ("trigger_to_record", "trigger", "record_start"),
                       ("endpoint_wait", "speech_end", "record_end"), ("stt", "stt_start", "stt_end"),
                       ("brain", "stt_end", "brain_end"), ("tts_first_audio", "brain_end", "first_audio"),
                       ("total", "speech_end", "first_audio")):
        g = _gap(m, a, b)
        if g is not None:
            stages[name] = g
    if "stt_end" not in m:                                          # a typed turn: the brain starts at the trigger
        g = _gap(m, "trigger", "brain_end")
        if g is not None:
            stages["brain"] = g
            g2 = _gap(m, "trigger", "first_audio")
            if g2 is not None:
                stages["total"] = g2
    rec = {"source": turn["source"], "outcome": outcome, "stages": stages, "llm": turn["llm"], "tools": turn["tools"], **turn["data"]}
    rec["llm_calls"] = len(turn["llm"])
    return rec


def describe(rec: dict) -> str:
    s = rec.get("stages", {})
    parts = [f"{k}={s[k]:.2f}s" for k in ("trigger_to_record", "endpoint_wait", "stt", "brain", "tts_first_audio") if k in s]
    llm = ", ".join(f"{c['provider']} {c['seconds']:.2f}s/{c['tools']} tools" for c in rec.get("llm", []))
    return (f"[{rec.get('source')}/{rec.get('outcome')}] " + " ".join(parts) + (f" | total={s['total']:.2f}s" if "total" in s else "")
            + (f" | llm: {llm}" if llm else "") + (" | RESUMED after pause" if rec.get("resumed") else ""))


timeline = Timeline()
