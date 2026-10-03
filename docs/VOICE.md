# Voice

`python main.py` starts the tray app. Press **Ctrl+Alt+V** (or click the pill, or tray > Listen now), say a command, stop talking:
Vex records until about one second of silence, sends the audio to Groq Whisper, runs the agent, and speaks the reply while the pill
shows the voice level. The typed prompt is still there: tray > **Type a command**, and it is the automatic fallback when speech
can't be used.

## What happens, step by step
1. **Recorder** (`voice/recorder.py`): 16 kHz mono, 30 ms frames, `webrtcvad` end-of-speech detection (energy fallback), 0.5 s pre-roll,
   gives up after 6 s of silence, 20 s maximum. Handles the laptop mic array (4 channels, 44.1/48 kHz) by down-mixing and resampling.
   Audio stays in memory and is never saved.
2. **Whisper** (`voice/stt.py`): Groq `whisper-large-v3-turbo`, English by default. Set `language = ""` in `[voice]` to auto-detect
   (Hindi, Kannada, ...). Seconds of audio are counted per hour in `data/voice_usage.json`; when the allowance is used up, or Groq
   says 429, Vex tells you when it frees up and lets you type instead.
3. **Speech** (`voice/tts.py`, `voice/speaker.py`): Google Chirp 3 HD if `google-tts-key.json` exists, otherwise edge-tts (free, no
   key). Sentences are synthesised while the previous one plays. Replies longer than `spoken_max_chars` (280): the first two sentences
   are spoken, then "The rest is on screen. Say 'read the rest' to hear it." Saying **"read the rest"**, "say the rest" or "read it
   all" speaks the remainder without asking the LLM.
4. **Barge-in**: press the hotkey while Vex speaks: speech stops at once and it listens.

## Confirmations by voice
- **Yellow** (for example "Lock the PC"): Vex shows the dialog *and* asks aloud. For 6 seconds after it finishes asking you can say
  "yes", "yeah", "yep", "do it", "go ahead" (or "no", "cancel", "stop", "nope"). Any other words count as **no**. Hearing nothing
  leaves the dialog open (its own timeout is no). A click works at any time; the first answer wins.
- **Red** and the typed CONFIRM step are **never** answered by voice: Vex only says "I need your approval on screen." The executor also
  rejects a voice answer to them as a second lock.
- Turn it off with `confirm_by_voice = false`.

## Google Cloud TTS (later, when you have the key)
1. Use the Google project **with billing** (not the AI Studio project) and set a budget alert.
2. Enable "Cloud Text-to-Speech API", create a service account, download its JSON key.
3. Save it as `google-tts-key.json` in the Vex folder (it is git-ignored). Restart Vex.

Vex then uses Chirp 3 HD first, counts characters per month (`data/voice_usage.json`), and switches to edge-tts at 90% of
`tts_monthly_free_chars` (check the current free allowance in your Google console) or on any error. The configured voice name is
checked against Google's voice list on first use; a wrong name falls back to edge-tts with a log message listing valid voices.
Built and tested with a fake Google client; it has not been run against the real service yet.

## Troubleshooting
- `python tools_dev/mic_check.py` (add `--transcribe` to see what Whisper gets): shows levels, whether speech was detected, and
  explains permission problems. `--list` shows input devices; set `input_device = "Intel"` (part of a name) in `[voice]` to choose one.
- Windows must allow desktop apps to use the microphone: Settings > Privacy & security > Microphone.
- No sound: check `output_device` in `[voice]`; `logs/vex.log` says which TTS provider answered.
- Limits and usage live in `data/voice_usage.json` (counts and timestamps only, never audio or text).
