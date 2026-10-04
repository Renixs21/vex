# Health check

```
python tools_dev\health.py            # free: no API quota used
python tools_dev\health.py --live     # also one tiny request per model + 1 s of speech recognition (asks first)
```
Or tray > **Health check**: the free run in the background, a toast with the verdict, then `logs\health.txt` opens.

Every line is PASS / WARN / FAIL / SKIP in plain words, with what to do under each WARN and FAIL. Exit code 1 if anything FAILs.

| Check | How (free run) |
|---|---|
| API keys | which key *names* are set (values are never shown) |
| Brain #1..#3 | each provider in `[llm] order`: is the key accepted and does the model exist? A model listing / lookup, which generates nothing and costs no tokens |
| Speech recognition | the Whisper model is available to your Groq key (lookup, no audio sent) |
| Speech output | edge-tts says a test word (free, no key); the offline Windows voice is available |
| Microphone | 1 s of sound: opens, not muted in Windows, not pure digital silence. A quiet room passes; speak during the check to see your level |
| Wake word | the model file loads |
| Vex app, Hotkey, Listening, Orb, Reminder scheduler | from `data\status.json`, the heartbeat the running Vex writes every 30 s (from the tray: the live state) |
| Memory | note count, the search index opens and passes SQLite's quick check |
| Reminders | the reminders file reads; pending / done |
| Disk space, Logs, Vault size | free space (warn < 2 GB), log folder size (warn > 100 MB), vault vs its 200 MB soft cap (warn at 80%) |
| Start with Windows | on/off |

`--live` (asks first, `--yes` skips the question): one generation of at most a few tokens per provider (about 20-60 tokens each with the
prompt) and 1 s of audio to Groq Whisper (Groq bills short audio as up to 10 s of the 7200 s/hour).

**Keys never appear**: every line goes through a redactor that hides every value from `.env` and the environment whose name looks like a
key/token/secret, plus anything key-shaped (`gsk_…`, `AIza…`, `sk-…`, `tvly-…`), even inside a provider's error message.
