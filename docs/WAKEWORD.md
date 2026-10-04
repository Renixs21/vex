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
| `threshold` | `0.5` (0.3 with the first trained model) | 0.05-0.99. Raise to 0.6-0.7 if it wakes by accident, lower if it misses you |
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

## How the "Hey Vex" model was trained (locally, no Colab)
Trained on this PC (CPU only) with [livekit-wakeword](https://github.com/livekit/livekit-wakeword) 0.3.0 (Apache-2.0; same mel and
embedding front end as openWakeWord, so its ONNX head loads in openWakeWord's `Model`). The negative-feature set (ACAV100M) makes the
model non-commercial; fine for personal use. Work folder: `C:\Users\<you>\wakeword_train\` (outside the repo); only `models/hey_vex.onnx`
is used by Vex. The scripts are in `tools_dev/wakeword_train/`.

1. `python -m venv lk`, `pip install "livekit-wakeword[train,eval,export]"`, `winget install eSpeak-NG.eSpeak-NG` (needed on PATH).
2. `hey_vex.yaml`: `target_phrases: ["hey vex"]`, 23 adversarial negatives ("vex", "hex", "hey alex", "hey tax", "hey jarvis"...),
   `n_samples` 3000 (first pass) or 8000, `model_type: conv_attention`, `small`, `steps` 12000 or 30000.
3. `livekit-wakeword setup` downloads 17 GB; instead `partial_acav.py 2.5` fetches only the first 2.5 GB of the 16 GB ACAV file with
   parallel HTTP Range requests and writes a valid smaller .npy (then use the library's `_download_validation_features`, `_download_rirs`,
   `_download_musan_noise` helpers for the rest).
4. `livekit-wakeword generate hey_vex.yaml` (Piper TTS on CPU, about 1.5 clips/s).
5. Your own voice: `python tools_dev/record_wakeword_clips.py --set train` (50+ "hey vex", 240+ negatives), then `mix_real.py`
   copies them into `positive_train` / `negative_train` (positives trimmed and repeated 30x, negatives 8x). Record a separate `--set heldout`
   for testing only; it is never trained on.
6. `livekit-wakeword augment` (its feature step crashes in 0.3.0, so run `extract_run.py` afterwards), then `train_run.py hey_vex.yaml`
   (about 12 steps/s on a 10-core CPU: the 14,400-step first pass took 18 minutes; it snapshots weights every 4 minutes), then
   `livekit-wakeword export hey_vex.yaml` (set `PYTHONUTF8=1` on Windows).
7. `eval_heldout.py output/hey_vex/hey_vex.onnx` scores your held-out clips through openWakeWord. Copy the .onnx to `models/hey_vex.onnx`.

**First model (3000 samples, 12000 steps, 46 real positives):** held-out recall 46% at threshold 0.3 with 0 false wakes in 63 negative
clips (54% at 0.2 with 4). Real data is what matters most: record more "hey vex" clips and retrain.

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
