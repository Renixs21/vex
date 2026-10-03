"""Voice answers to confirmations: yellow only, short window, strict phrases.

  * Yellow, yes/no confirmations: Vex shows the dialog AND speaks the question. After it finishes asking there are
    `window_seconds` (default 6) in which a clear "yes" / "no" is accepted; a click on the dialog works at any time. Whichever
    comes first wins.
  * Only these phrases count. Anything else that is heard counts as NO ("unclear = no"). Hearing nothing leaves the dialog open
    (its own timeout is no).
  * Red and typed (CONFIRM) confirmations are never voice: Vex only says "I need your approval on screen" and the dialog does the
    rest. (The executor also rejects any voice answer to a red or typed request, as a second lock.)
  * No microphone, no speech recognition, or no voice at all: it quietly behaves like the dialog alone.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable

from safety.confirm import Confirmation, Confirmer, ConfirmRequest
from voice.recorder import MicUnavailable
from voice.stt import SttError

log = logging.getLogger("vex.voiceconfirm")

YES = frozenset({"yes", "yeah", "yep", "yup", "yes please", "do it", "yeah do it", "yes do it", "go ahead", "yes go ahead",
                 "yeah go ahead", "please do", "yes please do it"})
NO = frozenset({"no", "nope", "no thanks", "no thank you", "cancel", "stop", "dont", "do not", "dont do it", "never mind",
                "cancel that", "stop that", "no dont"})


def parse_yes_no(text: str) -> str:
    """'yes', 'no' or 'unclear': exact clear phrases only, case and punctuation ignored."""
    t = " ".join(re.sub(r"[^a-z\s]", "", (text or "").lower().replace("'", "")).split())
    if t in YES:
        return "yes"
    if t in NO:
        return "no"
    return "unclear"


def question_for(req: ConfirmRequest) -> str:
    first = (req.detail.strip().splitlines() or [req.title])[0].strip().rstrip(".:")
    return f"{first[:140]}. Should I go ahead?"


class VoiceConfirmer:
    def __init__(self, dialog: Confirmer, speaker, recorder, stt, window_seconds: float = 6.0,
                 on_level: Callable[[float], None] | None = None, enabled: Callable[[], bool] = lambda: True,
                 is_muted: Callable[[], bool] = lambda: False):
        self.dialog = dialog
        self.speaker = speaker
        self.recorder = recorder
        self.stt = stt
        self.window = window_seconds
        self.on_level = on_level or (lambda v: None)
        self.enabled = enabled
        self.is_muted = is_muted

    # ---- the Confirmer interface ------------------------------------------------------------------------------------
    def confirm(self, req: ConfirmRequest) -> Confirmation:
        voice_ok = (req.tier == "yellow" and req.kind == "yes_no" and not req.requires_non_voice and self.enabled()
                    and not self.is_muted() and getattr(self.stt, "available", True))
        first: dict = {}
        won = threading.Event()
        lock = threading.Lock()
        stop_voice = threading.Event()

        def settle(source: str, answer: Confirmation) -> None:
            with lock:
                if "answer" in first:
                    return
                first["answer"], first["source"] = answer, source
            won.set()

        def run_dialog() -> None:
            settle("dialog", self.dialog.confirm(req))

        threading.Thread(target=run_dialog, name="confirm-dialog", daemon=True).start()
        worker = threading.Thread(target=self._voice if voice_ok else self._hint, args=(req, settle, won, stop_voice),
                                  name="confirm-voice", daemon=True)
        worker.start()
        won.wait(req.timeout + 20)
        if "answer" not in first:
            settle("timeout", Confirmation(False, "none", "timeout"))
        stop_voice.set()
        try:
            self.speaker.stop()
        except Exception:  # noqa: BLE001
            pass
        if first["source"] == "voice" and hasattr(self.dialog, "resolve_active"):
            self.dialog.resolve_active(Confirmation(False, "dialog", "answered by voice"))      # close the dialog that is still open
        worker.join(timeout=3)
        return first["answer"]

    # ---- the voice side -------------------------------------------------------------------------------------------------
    def _hint(self, req, settle, won, stop) -> None:
        """Red / typed / voice-off: no listening at all. Just point at the screen (red) and leave."""
        if req.requires_non_voice or req.kind == "typed":
            try:
                self.speaker.say("I need your approval on screen.", self.on_level)
            except Exception:  # noqa: BLE001
                log.debug("could not speak the on-screen hint", exc_info=True)

    def _voice(self, req, settle, won, stop) -> None:
        try:
            self.speaker.say(question_for(req), self.on_level)
        except Exception:  # noqa: BLE001 - no speech output: the dialog still works
            log.debug("could not speak the question", exc_info=True)
        if won.is_set() or stop.is_set():
            return
        try:
            rec = self.recorder.record(stop, no_speech_seconds=self.window, max_seconds=self.window + 4)
            if won.is_set() or rec.reason in ("cancelled", "no_speech") or len(rec.audio) == 0:
                return                                                          # nothing heard: the dialog stays in charge
            text = self.stt.transcribe(rec.audio)
        except (MicUnavailable, SttError) as e:
            log.info("voice confirmation unavailable (%s); using the dialog only", e)
            return
        except Exception:  # noqa: BLE001
            log.exception("voice confirmation failed")
            return
        if won.is_set() or not text:
            return
        verdict = parse_yes_no(text)
        log.info("voice confirmation heard a %s answer", verdict)                # what was said is not logged
        if verdict == "yes":
            settle("voice", Confirmation(True, "voice"))
        else:                                                                    # "no", or anything unclear, counts as no
            settle("voice", Confirmation(False, "voice", "declined" if verdict == "no" else "unclear"))
