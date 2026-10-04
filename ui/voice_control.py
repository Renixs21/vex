"""Voice settings the Control Center can read and change, live: wake word sensitivity (with a live mic meter), the follow-up window,
and Vex's voice. Changes take effect at once and are saved to config.toml (the same keys you can edit by hand)."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("vex.voice_control")

THRESHOLD_RANGE = (0.05, 0.95)
FOLLOWUP_RANGE = (0, 30)
PREVIEW_TEXT = "Hi, I'm Vex. This is how I'll sound."


class VoiceControl:
    def __init__(self, controller, wake=None, stack=None, set_value: Callable[[str, str, Any], None] | None = None,
                 settings: dict | None = None):
        self.controller = controller
        self.wake = wake                       # voice.wakeword.WakeWordListener or None
        self.stack = stack                     # voice.factory.VoiceStack or None (voice off)
        if set_value is None or settings is None:
            import config

            set_value = set_value or config.set_toml_value
            settings = settings or config.settings
        self._save = set_value
        self.settings = settings
        self._preview_lock = threading.Lock()

    # ---- reading ---------------------------------------------------------------------------------------------------------------------
    def _edge_providers(self) -> list:
        chain = getattr(self.stack, "chain", None)
        return [p for p in getattr(chain, "providers", []) if getattr(p, "name", "") == "edge"]

    def current_voice(self) -> str:
        provs = self._edge_providers()
        return getattr(provs[0], "voice", "") if provs else str(self.settings["voice"].get("edge_voice", ""))

    def get(self) -> dict:
        v, w = self.settings["voice"], self.settings.get("wakeword", {})
        return {"wake": {"available": self.wake is not None, "enabled": bool(w.get("enabled", False)),
                         "threshold": float(getattr(self.wake, "threshold", w.get("threshold", 0.5))),
                         "label": getattr(self.wake, "label", ""), "range": list(THRESHOLD_RANGE)},
                "followup": {"enabled": bool(getattr(self.controller, "followup_enabled", v.get("followup_enabled", True))),
                             "seconds": float(getattr(self.controller, "followup_seconds", v.get("followup_s", 8))),
                             "range": list(FOLLOWUP_RANGE)},
                "voice": {"current": self.current_voice(), "candidates": list(v.get("edge_candidates", [])),
                          "available": self.stack is not None},
                "silence_ms": int(v.get("silence_ms", 700))}

    def levels(self) -> dict:
        """The live mic level and wake word score (polled a few times a second while the Voice screen is open)."""
        w = self.wake
        paused = getattr(w, "paused", False)                   # a property on WakeWordListener
        paused = paused() if callable(paused) else paused
        listening = bool(w is not None and getattr(w, "running", False) and not paused)
        return {"level": round(float(getattr(w, "last_level", 0.0)), 3), "score": round(float(getattr(w, "last_score", 0.0)), 3),
                "threshold": float(getattr(w, "threshold", 0.5)), "listening": listening}

    # ---- changing ------------------------------------------------------------------------------------------------------------------------
    def set(self, args: dict) -> dict:
        changed = []
        if "threshold" in args:
            t = float(args["threshold"])
            if not THRESHOLD_RANGE[0] <= t <= THRESHOLD_RANGE[1]:
                raise ValueError(f"sensitivity must be between {THRESHOLD_RANGE[0]} and {THRESHOLD_RANGE[1]}")
            if self.wake is not None:
                self.wake.threshold = t
            self._save("wakeword", "threshold", round(t, 3))
            changed.append(f"wake threshold {t:.2f}")
        if "followup_seconds" in args:
            s = float(args["followup_seconds"])
            if not FOLLOWUP_RANGE[0] <= s <= FOLLOWUP_RANGE[1]:
                raise ValueError(f"the follow-up window must be {FOLLOWUP_RANGE[0]}-{FOLLOWUP_RANGE[1]} seconds")
            self.controller.followup_seconds = s
            self._save("voice", "followup_s", int(s) if s == int(s) else s)
            changed.append(f"follow-up {s:g} s")
        if "followup_enabled" in args:
            on = bool(args["followup_enabled"])
            self.controller.followup_enabled = on
            self._save("voice", "followup_enabled", on)
            changed.append("follow-up " + ("on" if on else "off"))
        if "voice" in args:
            name = str(args["voice"])
            if name not in self.settings["voice"].get("edge_candidates", []):
                raise ValueError("pick one of the listed voices")
            for p in self._edge_providers():
                p.voice = name
                fb = getattr(p, "fallback", None)
                if fb is not None and hasattr(fb, "voice"):
                    fb.voice = name
            self._save("voice", "edge_voice", name)
            changed.append(f"voice {name}")
        if changed:
            log.info("voice settings changed from the Control Center: %s", ", ".join(changed))
        return self.get()

    def preview(self, voice: str) -> dict:
        """Say a sample sentence in `voice` (only while Vex is idle; it doesn't change the voice)."""
        if self.stack is None:
            raise ValueError("voice is off")
        if voice not in self.settings["voice"].get("edge_candidates", []):
            raise ValueError("pick one of the listed voices")
        if self.controller.state != "idle" or not self._preview_lock.acquire(blocking=False):
            raise ValueError("Vex is busy right now; try again in a moment")

        def run() -> None:
            try:
                from voice.tts import EdgeTTS

                audio = EdgeTTS(voice).synthesize(PREVIEW_TEXT)
                self.stack.speaker.player.play(audio.samples, audio.rate, lambda v: None)
            except Exception:  # noqa: BLE001
                log.exception("voice preview failed")
            finally:
                self._preview_lock.release()

        threading.Thread(target=run, name="voice-preview", daemon=True).start()
        return {"previewing": voice}
