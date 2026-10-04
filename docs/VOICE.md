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
3. **Speech** (`voice/tts.py`, `voice/speaker.py`): **edge-tts** (Microsoft's neural voices: free, no key, needs internet), then the
   **offline Windows voice** (no internet, plainer) if edge-tts is unreachable, so Vex is never silent. Google Chirp is optional and off
   by default (see the last section). Sentences are synthesised while the previous one plays. Replies longer than `spoken_max_chars`
   (280): the first two sentences are spoken, then "The rest is on screen. Say 'read the rest' to hear it." Saying **"read the rest"**,
   "say the rest" or "read it all" speaks the remainder without asking the LLM.
4. **Barge-in**: press the hotkey while Vex speaks: speech stops at once and it listens.

## Confirmations by voice
- **Yellow** (for example "Lock the PC"): Vex shows the dialog *and* asks aloud. For 6 seconds after it finishes asking you can say
  "yes", "yeah", "yep", "do it", "go ahead" (or "no", "cancel", "stop", "nope"). Any other words count as **no**. Hearing nothing
  leaves the dialog open (its own timeout is no). A click works at any time; the first answer wins.
- **Red** and the typed CONFIRM step are **never** answered by voice: Vex only says "I need your approval on screen." The executor also
  rejects a voice answer to them as a second lock.
- Turn it off with `confirm_by_voice = false`.

## Choose the edge-tts voice
`edge_voice` in `[voice]` (default `en-US-AriaNeural`). To hear the candidates (`edge_candidates`: three Indian-English, nine US and
three UK voices) and pick one:
```
python tools_dev/tts_voices.py                       # one sentence in each, speaking its own name: "Hi, I'm Neerja. This is how I sound..."
python tools_dev/tts_voices.py --voices Neerja Ava   # just these (a full name, or part of one)
python tools_dev/tts_voices.py --list --locale en-IN # every voice edge-tts offers (any language prefix: en-GB, hi-IN, ...)
python tools_dev/tts_voices.py --windows             # also the offline Windows voice
python tools_dev/tts_voices.py --set Neerja          # save your pick in config.toml, then restart Vex
```
Each run needs internet and costs nothing. Add your own names to `edge_candidates` if you want to compare others.

## Google Chirp (optional, off by default)
Vex does not need Google: with the default `tts_order = ["edge", "windows"]` no Google key is read and nothing about Google appears at
startup. Chirp 3 HD voices sound better but need a billing-enabled Google Cloud project (Google may ask for a prepayment or a card). To use them,
get a key as below, then set `tts_order = ["google", "edge", "windows"]` in `config.toml`; the ladder below then applies.
**Free tier:** Chirp 3 HD includes **1,000,000 characters per month free**, then about $30 per million (check Google's pricing page, it can
change). Vex speaks at most two sentences of up to 280 characters per reply, so a month is thousands of replies. Google counts the characters
of the text it speaks. Billing must be enabled on the project even to use the free allowance.

**Get a key** (use a *separate* project from the Gemini key: billing on the Gemini project would end its free tier):
1. console.cloud.google.com > create a new project (for example "vex-tts").
2. Billing: link a billing account, then Billing > Budgets & alerts > create a budget of about $1 with alerts at 50%, 90%, 100%.
   (A budget alert only emails you; it does not stop charges. Vex's own counter below is the first line of defence.)
3. APIs & Services > Library > "Cloud Text-to-Speech API" > Enable.
4. Credentials, either:
   - **API key** (simplest): APIs & Services > Credentials > Create credentials > API key > Restrict key > API restrictions > *Cloud
     Text-to-Speech API* only. Put it in `.env` (git-ignored) as `GOOGLE_TTS_API_KEY=...`.
   - **Service account:** IAM & Admin > Service accounts > create > Keys > Add key > JSON. Save it as `google-tts-key.json` in the Vex
     folder (git-ignored). Used only when no API key is set.
5. Restart Vex. The key is never written to the repo or the logs (the API key travels in a request header, not in the URL).

**What happens when things go wrong** (a failure never leaves Vex silent):
| Situation | Behaviour |
|---|---|
| no key | edge-tts, nothing counted |
| network blip / 5xx | next voice now; Google is tried again after a minute |
| quota or billing error (HTTP 429, "quota exceeded") | next voice for an hour (`tts_quota_pause_seconds`); one tray message |
| key rejected, API not enabled, billing off (400/401/403) | Google switched off for this session; one tray message saying what to check |
| edge-tts offline too | the Windows voice speaks (offline) |
| all three fail | the text is shown on screen only |

**Staying inside the free tier:** characters are counted per month in `data/voice_usage.json`. Vex warns in the tray at **75%** and **90%**
(`tts_warn_fractions`, once each per month, not repeated after a restart) and switches to edge-tts at 90% (`tts_switch_fraction`), or earlier
if a sentence would pass the limit. A new month starts from zero. This is a local count: your Google budget alert remains the real backstop.
`python tools_dev/tts_voices.py --usage` shows the numbers.

**Choose the voice:** `google_voice` in `[voice]` (default `en-US-Chirp3-HD-Aoede`). To hear the candidates (`google_candidates`: Aoede, Puck,
Charon, Kore, Fenrir, Leda, Orus, Zephyr):
```
python tools_dev/tts_voices.py --google         # one sentence in each (about 60 characters each, counted like normal speech)
python tools_dev/tts_voices.py --google --voices Kore
python tools_dev/tts_voices.py --google --list
python tools_dev/tts_voices.py --google --set Kore
```
The configured name is checked against Google's list on first use; a wrong name falls back to edge-tts with a message listing valid voices.
Built and tested with the network mocked (no real Google call has been made yet): your first real test is `tts_voices.py`.

## Troubleshooting
- `python tools_dev/mic_check.py` (add `--transcribe` to see what Whisper gets): shows levels, whether speech was detected, and
  explains permission problems. `--list` shows input devices; set `input_device = "Intel"` (part of a name) in `[voice]` to choose one.
- Windows must allow desktop apps to use the microphone: Settings > Privacy & security > Microphone.
- No sound: check `output_device` in `[voice]`; `logs/vex.log` says which TTS provider answered.
- Limits and usage live in `data/voice_usage.json` (counts and timestamps only, never audio or text).

## Latency: where the time goes
Every voice turn is timed from the moment you stop talking to Vex's first spoken word, stage by stage, and written to `logs/latency.jsonl`
(one line per turn: timings, provider names, counts and whether your sentence ended with a full stop; never what you said). Also in `vex.log`
as `TURN TIMING ...`.

```
python tools_dev\latency_report.py              # your last 30 turns: table, medians / 90th percentile per stage, cut-off statistics
python tools_dev\latency_report.py --last 10    # only your last 10 turns
python tools_dev\latency_report.py --since "2026-10-04 17:30"
```
Stages: `endpoint_wait` (the silence Vex waits for), `stt` (upload + Whisper), `brain` (model calls and tools), `tts_first_audio` (reply ready ->
first sound), `total`. The wake word adds `wake_to_trigger` and `trigger_to_record`.

**The silence endpoint** (`[voice] silence_ms`, now 700; it was 1000) is the fixed wait in every turn. Lower is faster but cuts slow talkers off.
**The mid-sentence-pause safeguard** (`resume_grace_ms = 500`, `resume_min_ms = 120`): after the endpoint the microphone stays open for 500 ms more
while speech recognition is already running. If you carry on talking in that window, the recording is extended and both parts are transcribed
together (the report counts these as "resumed": turns that would have been cut off). If you don't, nothing is lost: no extra time is spent.
A pause longer than the window can still split a sentence; the report lists turns that did not end like a sentence and were followed by
another turn within 15 seconds as "possible residual cut-offs". Don't go below 700 ms without looking at these numbers.

## Speed: streaming speech, warm connection, speech-recognition deadline
* **Sentence streaming** (already in place, unchanged): the reply is split into sentences; sentence 1 is synthesized and played at once while
  the rest are synthesized in the background and queue behind it. Barge-in cancels the queue.
* **Warm edge-tts** (`edge_warm = true`): plain edge-tts opens a new connection for every sentence (about 0.7 s handshake + 0.4 s synthesis).
  Vex now keeps one connection open, opens it at the start of every turn, and reuses it: measured 0.3-0.5 s per sentence instead of 1.3-1.4 s.
  If the connection drops or anything fails, that sentence goes through plain edge-tts; if edge-tts internals ever change, warm switches itself off.
* **Speech recognition deadline** (`stt_timeout_seconds = 3.0` + 0.25 s per second of audio): Groq Whisper gets one retry on a timeout or server
  error, then the same audio goes to Gemini (`stt_fallback = "gemini"`, same free key as the brain; `""` disables). A 429 goes straight to the backup.
* `latency.jsonl` now records `audio_seconds`, `stt_attempts`, `stt_provider`, `stt_fallback`; the report prints audio length next to STT time.

## Follow-up listening
After Vex answers something you **said**, it keeps listening for `[voice] followup_s` seconds (default 8) without "Hey Vex" or the hotkey.
The orb stays in its listening look (so it doesn't dock), the caption says "Still listening…", the tray says "Still listening (no wake word
needed)". Speak and it is a normal turn (same endpointing, same pipeline), after which the window opens again. Silence: the window closes
quietly and Vex is back to wake-word-only mode (the orb's auto-sleep timer then runs as usual).

* **Not its own voice:** the window opens only after playback has fully ended plus `followup_delay_ms` (300), and needs
  `followup_min_speech_ms` (250 ms; a normal turn needs 150) of speech before it counts. A quiet room in the window never triggers the
  "Vex can't hear you" notice, and a missing mic/key never pops up the typing box.
* **No window** after: silence, an error, a typed command, a reminder, "sleep", any turn where Vex asked a confirmation question (that flow
  has its own input), while muted, or with `followup_enabled = false` / `followup_s = 0` / tray > **Follow-up listening** off.
* **Ending it:** "that's all", "thanks Vex", "thank you", "stop listening", "nothing else", "never mind", "I'm done" said in the window close
  it at once, without a reply, and park the orb. The hotkey, a typed command or a click during the window end it and start a normal turn;
  a reminder closes it and is spoken; muting closes it.
* `latency.jsonl` has `"followup": true/false`; follow-up turns show as `followup` in the report's "via" column, and the report counts
  windows opened / used / closed in silence / ended by a phrase.

Why this was needed: after a reply only the wake-word scorer was re-armed (`on_interaction_end` -> `WakeWordListener.resume`, with a
0.6 s settle), the recorder itself only opens inside a turn, so anything said without "Hey Vex" was scored for the wake phrase and dropped.
