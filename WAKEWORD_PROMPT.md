# Task: "Hey Vex" wake word

Add always-on wake-word activation to Vex. When I say **"Hey Vex"**, Vex should wake up, show the orb as listening, record my command, and handle it exactly as if I had pressed the hotkey.

Read `CLAUDE.md` first for the overall project. This task covers only the wake word. Show me your plan before writing code, and ask if anything conflicts with what's already built.

---

## 1. What already exists

- `voice/wakeword.py` is **provided — keep its public API**. You may fix bugs or add features, but don't rewrite it from scratch.

  ```python
  listener = WakeWordListener(on_wake=callback, model_path="models/hey_vex.onnx",
                              fallback_model="hey_jarvis", threshold=0.5,
                              cooldown=2.0, vad_threshold=0.5, device=None)
  listener.start()   # background thread, always listening
  listener.pause()   # while Vex records or speaks
  listener.resume()  # afterwards (resets the model buffer)
  listener.stop()
  ```

- It uses **openWakeWord** (local, free, no API key). It loads `models/hey_vex.onnx`; if that file doesn't exist yet, it falls back to the built-in `hey_jarvis` model so everything can be tested now.
- `setup.ps1` already installs `openwakeword` + `onnxruntime` and downloads the base models.

## 2. Integration requirements

1. **Config** — add a `[wakeword]` section to `config.toml`:
   ```toml
   [wakeword]
   enabled = true
   model_path = "models/hey_vex.onnx"
   fallback_model = "hey_jarvis"
   threshold = 0.5        # raise to 0.6-0.7 if it wakes by accident
   cooldown = 2.0         # seconds between triggers
   vad_threshold = 0.5
   input_device = ""      # blank = default mic
   chime = true
   ```
   Load it through `config.py`. Invalid values should log a warning and use defaults, not crash.

2. **One wake path.** Wake word, hotkey and clicking the orb must all call the **same** `start_listening()` function, so behavior is identical whichever I use.

3. **Wake sequence:**
   `wake detected → listener.pause() → chime → orb.setState('listening') → recorder → STT → 'thinking' → agent → TTS → 'speaking' → 'idle' → listener.resume()`

4. **Never self-trigger.** The listener must stay paused for the entire time Vex is recording, thinking or speaking, and resume only after the reply audio has fully finished. Use a `try/finally` so it always resumes, even if STT, the LLM or TTS fails.

5. **Mic sharing.** The wake-word stream and the command recorder must not fight over the microphone. Either pause the wake-word stream and open the recorder on the same device, or feed both from one shared input stream. Pick one, explain why, and make sure it works with Windows default-device changes (e.g. plugging in headphones) by reopening the stream on error.

   Keep a rolling ~1.5 s pre-buffer of audio so that words I say right after "Hey Vex" (before the recorder fully starts) aren't lost. Strip the wake phrase itself before sending audio to STT, or strip "hey vex" from the transcript.

6. **Chime.** Play a short, soft wake sound (generate a ~150 ms sine "blip" with numpy, no external file needed). Skip it if `chime = false`.

7. **Barge-in.** If I say "Hey Vex" while Vex is speaking... it's paused, so it won't hear. Keep the **hotkey** as the barge-in method: pressing it while Vex speaks stops the audio and starts listening.

8. **Tray menu toggle:** "Wake word: On/Off" (persist the choice to `config.toml`). Also show which model is active ("Hey Vex" or "Hey Jarvis (fallback)").

9. **Failure handling.** If the mic is missing or the listener crashes, log it, show a tray notification once ("Wake word unavailable — hotkey still works"), and retry every 30 seconds. Vex must keep running.

10. **Privacy.** Wake-word audio is processed in memory only. Never save it, log it or send it anywhere. Only audio recorded *after* waking goes to STT.

11. **CPU.** The listener should use well under 5% CPU on an average laptop. Measure it and tell me the number.

## 3. Calibration tool

Create `tools_dev/wakeword_tune.py` (a dev script, not an agent tool):

- Shows a live score bar in the terminal for the active model.
- Mode 1 — **"Say it" test:** asks me to say the wake phrase 10 times and reports how many were detected at the current threshold.
- Mode 2 — **false-trigger test:** listens for N minutes while I talk normally / play a video, and counts false wakes.
- Prints a suggested threshold based on both tests and offers to write it to `config.toml`.
- `--device` option and a `--list-devices` flag.

## 4. Training the custom "Hey Vex" model

I will train the model myself in Google Colab — **you don't train it**. Write `docs/WAKEWORD.md` explaining:

1. Open the openWakeWord automatic training notebook (find the current link in the openWakeWord GitHub README).
2. Set the target phrase to `hey vex`. Recommended settings for good accuracy on a free Colab GPU.
3. Download the resulting `.onnx` file, rename it `hey_vex.onnx`, put it in `models/`.
4. Restart Vex; the tray should show "Hey Vex" as the active model.
5. Run `wakeword_tune.py` to pick the threshold.
6. Troubleshooting: too many false wakes, misses from across the room, wrong microphone, headset vs laptop mic.

Also note in the doc: short wake words trigger falsely more often, so always train the full phrase "hey vex", never just "vex".

## 5. Tests

In `tests/test_wakeword.py` (no real microphone needed — mock `sounddevice` and the model):

- Falls back to `hey_jarvis` when `models/hey_vex.onnx` is missing.
- A score above threshold fires `on_wake` once; two quick triggers within the cooldown fire only once.
- While paused, high scores never fire.
- `resume()` resets the model.
- The wake sequence always calls `resume()` even when the handler raises.
- Invalid config values fall back to defaults.

## 6. Done when

- ✅ Saying "Hey Jarvis" (fallback) or "Hey Vex" (once trained) wakes Vex, plays the chime, and the orb switches to listening.
- ✅ "Hey Vex, open notepad" in one breath works — the command after the wake phrase is captured.
- ✅ Vex never wakes itself up while speaking.
- ✅ Watching a 10-minute YouTube video near the mic causes no more than 1 false wake at the chosen threshold.
- ✅ Unplugging the mic doesn't crash Vex; the hotkey still works.
- ✅ All tests pass.

When finished, tell me how to run the calibration tool and what CPU usage you measured.
