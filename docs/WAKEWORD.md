# Wake word: "Hey Vex"

Say the phrase and Vex wakes up: a soft blip, the orb turns to listening, you speak your command (in the same breath is fine:
"Hey Vex, open notepad"), and the turn runs exactly like a hotkey press. It uses [openWakeWord](https://github.com/dscripka/openWakeWord):
local, free, no key.

Until you train your own model (below) Vex uses the built-in **"Hey Jarvis"** model. The tray shows which one is active:
`Wake word: Hey Jarvis (fallback)` or `Wake word: Hey Vex`.

## How it behaves
- **One path.** Wake word, hotkey, tray "Listen now" and orb click all call `controller.start_listening()`. The log says which one:
  `trigger from wakeword -> started`.
- **Never wakes itself.** The listener pauses the moment it fires and stays paused while Vex records, thinks and speaks. It resumes
  (with the model's buffer reset and a short 0.6 s deaf period for echo) only after the reply has fully finished, in a `finally`, so a
  failing STT, LLM or TTS can't leave it deaf. To interrupt Vex mid-sentence use the hotkey (it can't hear "Hey Vex" while paused).
- **Microphone sharing.** The listener keeps its own input stream open and simply stops scoring while paused; the recorder opens its
  own stream for the command. Windows shares the microphone, and this avoids the delay and glitches of closing and reopening a stream
  on every turn. If the stream dies (unplugged, headphones plugged in, default device changed) it reopens it.
- **Words right after the wake phrase.** Audio that arrives after the detection (up to 1.5 s) is handed to the recorder as its first
  audio. Nothing from before the detection is kept, so the wake phrase isn't sent to speech recognition; a transcribed "Hey Vex" at
  the start is also stripped.
- **Privacy.** Wake-word audio is scored in memory only: never saved, logged or sent anywhere. Only audio recorded after waking goes to
  Whisper.
- **Failures.** No microphone or a crashed listener: one tray notification ("Wake word unavailable. The hotkey still works."), then a retry
  every 30 s. Vex keeps running.

## Settings (`[wakeword]` in config.toml)
| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | also the tray switch "Wake word" (saved here) |
| `model_path` | `models/hey_vex.onnx` | your trained model |
| `fallback_model` | `hey_jarvis` | used while `model_path` doesn't exist |
| `threshold` | `0.5` | 0.05-0.99. Raise to 0.6-0.7 if it wakes by accident, lower if it misses you |
| `cooldown` | `2.0` | seconds between wakes |
| `vad_threshold` | `0.5` | openWakeWord's own speech gate (0 = off) |
| `input_device` | `""` | blank = the `[voice]` microphone / Windows default |
| `chime` | `true` | the soft blip on wake |

An invalid value is logged (`[wakeword] threshold = 7 is not valid; using 0.5`) and replaced by its default.

## Calibrate
```
python tools_dev/wakeword_tune.py --list-devices
python tools_dev/wakeword_tune.py                     # live score bar; say the phrase and watch it cross the "|"
python tools_dev/wakeword_tune.py --say 10            # 10 attempts, counts detections
python tools_dev/wakeword_tune.py --quiet 5           # 5 minutes of normal talk / a video: counts false wakes
python tools_dev/wakeword_tune.py --say 10 --quiet 5 --write    # then offers to save the suggested threshold
```
`--device "Realtek"` (or an index) picks a microphone. The suggestion is the midpoint between your quietest real wake and the loudest
false one; if they overlap it favours fewer false wakes and tells you so.

## Train your own "Hey Vex" (in Google Colab, free)
You train it; Vex doesn't.
1. Open the **automatic model training** notebook linked from the openWakeWord README
   (https://github.com/dscripka/openWakeWord, section "Training New Models"; the link changes, so take it from there).
2. Runtime > Change runtime type > a free **GPU**.
3. Set the target phrase to **`hey vex`**. Always train the full phrase, never just "vex": short wake words trigger falsely far more
   often. Leave the other settings at their defaults first; if you get false wakes later, train again with more steps / more negative
   data (the notebook explains the knobs).
4. Run all cells (it generates synthetic speech, so it takes a while). Download the resulting `.onnx`.
5. Rename it `hey_vex.onnx` and put it in `models/` (this folder is git-ignored: `*.onnx`).
6. Restart Vex. The tray should now say `Wake word: Hey Vex`.
7. Run `wakeword_tune.py --say 10 --quiet 5` and set the threshold it suggests.

## Troubleshooting
- **False wakes**: raise `threshold` to 0.6-0.7, raise `cooldown`, or retrain with more negative data. TV and music can do it; the
  quiet test shows how often.
- **Misses, or it doesn't hear you from across the room**: lower `threshold` a little (0.4), move closer, check the mic level in
  Windows Sound settings, and check `peak RMS` in `logs/vex.log` after a hotkey turn (near 0 means the mic is silent).
- **Wrong microphone**: `--list-devices`, then set `input_device = "part of name"`.
- **Headset vs laptop mic**: a headset is much cleaner. Plugging one in changes the default device; the listener notices the old stream
  stopped and reopens on the new default within a few seconds. If your headset's microphone isn't the default, set `input_device`.
- **Nothing happens**: check `logs/vex.log` for `wake word listening`, `wake detected`, `wake word unavailable`.

## Measured
- **CPU while listening (idle, real microphone, 2 minutes):** about 9% of one core, which is 0.8% of total CPU on a 12-thread laptop CPU
  (about 2% on a 4-core one). Most of it is the neural-network scoring every 80 ms.
- **False wakes, 2 minutes of a quiet room with the real mic:** 0 (the room was very quiet, so this is a smoke test, not a stress test;
  use `wakeword_tune.py --quiet 10` with a video playing for a real number).
- **End to end with a simulated microphone** (`python tools_dev/wakeword_sim.py`, speaks aloud): "Hey Jarvis what's the time" in one
  breath was detected (score 0.92-0.97), recorded, transcribed as "What's the time?", answered and spoken; and with "Hey Jarvis" injected
  into the microphone at full strength while Vex spoke a 3-sentence and a 6-sentence reply, it did not wake itself.
