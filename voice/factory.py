"""Builds the voice stack (recorder, Whisper, TTS chain, speaker, capture, confirmer) from the [voice] section of config.toml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import config
from safety.confirm import Confirmer
from ui.levels import SoundDevicePlayer
from voice.capture import VoiceCapture
from voice.confirm import VoiceConfirmer
from voice.recorder import Recorder
from voice.speaker import VoiceSpeaker
from voice.stt import GroqWhisper
from voice.tts import EdgeTTS, GoogleTTS, TTSChain, WindowsTTS
from voice.usage import UsageStore


@dataclass
class VoiceStack:
    recorder: Recorder
    stt: GroqWhisper
    chain: TTSChain
    player: SoundDevicePlayer
    speaker: VoiceSpeaker
    capture: VoiceCapture
    usage: UsageStore
    cfg: dict

    def wrap_confirmer(self, dialog: Confirmer, is_muted: Callable[[], bool] = lambda: False,
                       on_level: Callable[[float], None] | None = None) -> Confirmer:
        """The dialog confirmer, plus spoken questions and spoken yes/no for yellow ones (if [voice] confirm_by_voice)."""
        if not self.cfg["confirm_by_voice"]:
            return dialog
        return VoiceConfirmer(dialog, self.speaker, self.recorder, self.stt, window_seconds=float(self.cfg["confirm_voice_seconds"]),
                              on_level=on_level, is_muted=is_muted)


def build_voice(cfg: dict | None = None, root: Path | None = None, on_level: Callable[[float], None] | None = None,
                notify: Callable[[str, str], None] | None = None, typed_fallback: Callable[[], str | None] | None = None) -> VoiceStack:
    cfg = cfg or config.settings["voice"]
    root = root or config.ROOT
    usage = UsageStore(root / "data" / "voice_usage.json")
    recorder = Recorder(device=cfg["input_device"], silence_ms=cfg["silence_ms"], max_seconds=cfg["max_seconds"],
                        no_speech_seconds=cfg["no_speech_seconds"], preroll_ms=cfg["preroll_ms"], on_level=on_level,
                        vad_aggressiveness=cfg["vad_aggressiveness"], grace_ms=int(cfg.get("resume_grace_ms", 0)),
                        resume_ms=int(cfg.get("resume_min_ms", 120)))
    stt = GroqWhisper(api_key=config.env("GROQ_API_KEY"), model=cfg["stt_model"], language=cfg["language"],
                      prompt=cfg.get("stt_prompt", ""), usage=usage, seconds_per_hour=cfg["stt_seconds_per_hour"])
    key = root / config.env("GOOGLE_APPLICATION_CREDENTIALS", "google-tts-key.json")
    makers = {"google": lambda: GoogleTTS(cfg["google_voice"], cfg["google_language"], credentials=key,
                                          api_key=config.env("GOOGLE_TTS_API_KEY")),
              "edge": lambda: EdgeTTS(cfg["edge_voice"]),
              "windows": lambda: WindowsTTS(cfg.get("windows_voice", ""))}
    providers = [makers[n]() for n in cfg["tts_order"] if n in makers] or [EdgeTTS(cfg["edge_voice"])]
    if not any(p.name == "windows" for p in providers):          # whatever the order says, the offline voice is the last resort
        providers.append(WindowsTTS(cfg.get("windows_voice", "")))
    chain = TTSChain(providers, usage, monthly_limit=cfg["tts_monthly_free_chars"], switch_fraction=cfg["tts_switch_fraction"],
                     notify=notify, warn_fractions=tuple(cfg.get("tts_warn_fractions", (0.75, 0.9))),
                     quota_pause=float(cfg.get("tts_quota_pause_seconds", 3600)))
    player = SoundDevicePlayer(device=cfg["output_device"] or None)
    speaker = VoiceSpeaker(chain, player, max_chars=cfg["spoken_max_chars"], max_sentences=cfg["spoken_sentences"])
    capture = VoiceCapture(recorder, stt, fallback=typed_fallback, notify=notify)
    return VoiceStack(recorder, stt, chain, player, speaker, capture, usage, cfg)
