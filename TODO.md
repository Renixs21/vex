# Status and deferred work

## Done
- Phase 0: text agent, Gemini -> Groq failover (2 quick tries, then 5 minutes on the fallback, each switch logged).
- Phase 1: all section-5 tools, risk tiers, Confirmer, protected paths, audit log, dry-run, undo, memory tools registered
  (forget = yellow), memory context injected into the prompt.
- Phase 3, renderer-independent part: tray, global hotkey, native Qt confirmation dialogs (Confirmer), `VisualFrontend`
  interface + temporary Qt pill, controller (listen/think/speak, barge-in, hooks for the wake word), typed prompt standing in for
  the microphone, simulated speaker + level pump. See docs/FRONTEND_PROTOCOL.md and docs/SMOKE_TESTS.md.
- VexMind core: vault, index, retrieval, forget (Recycle Bin, three groups), provenance history on changed values.

- Speed/quality pass: tool groups (core + matching groups + more_tools), one-line descriptions, no SDK retries, retry-after failover,
  rolling-window rate limiter, Groq reasoning settings, reply filter (`brain/sanitize.py`), admin gating (`safety/elevation.py`),
  empty replies treated as failures. Measurements: docs/BENCHMARKS.md.

## Deferred
| Deferred | Needs | Unblocked by |
|---|---|---|
| Voice Confirmer (tray/hotkey/dialog Confirmers exist). A voice Confirmer MUST route `requires_non_voice` requests (every red request and the typed CONFIRM) through hotkey, tray click or typing, never speech; yellow may be voice. The executor already rejects a voice answer to those. | mic, hotkey, tray | Phases 2-3 |
| Read confirmations aloud (yellow may be answered by voice; red/typed never) | TTS, recorder | Phase 2 |
| Real microphone capture + STT replacing `ui/capture.TextPromptCapture`, real TTS replacing `SimulatedSpeaker` (use `ui.levels.SoundDevicePlayer`) | recorder, Groq STT, TTS | Phase 2 |
| Final visual (maybe a separate Godot app): socket transport for `JsonLinesFrontend`, `click` message back to Python | your design choice | later |
| Wake-word listener: the tray switch and the controller hooks (`on_interaction_start/end`, `on_wakeword_toggle`) already exist | openWakeWord | wake-word task |
| `memory/curator.py` (post-conversation extraction, inbox, daily summaries) + its tests | the controller's idle hook (exists now) | next |
| `memory/consolidate.py` nightly job | curator + scheduler | Phase 5 |
| Memory viewer `ui/memory.html` (the tray item is a placeholder that opens the notes folder) | PySide6 window | next |
| Semantic search via an `Embedder` | embedding API choice | later |
| Wake word (`WAKEWORD_PROMPT.md`) | recorder, STT, TTS, orb, tray | Phases 2-3 |
| Reminders: tray "list/cancel" UI, voice announcement of a fired reminder | TTS | Phase 2 |

## Known limitations
- Undo covers file create / copy / move / delete only. Deleted or redacted memory facts can't be undone (notes sent to the
  Recycle Bin by `forget` can be restored by hand).
- The administrator path (UAC) is unit-tested with a mock; it has not been run for real, because it needs you to click UAC.
- `set_volume`, `set_brightness`, `lock_pc`, `shutdown_pc`, `restart_pc` and the UAC path are tested with mocks only: run docs/SMOKE_TESTS.md once.
- `shell_rules` is a safety net, not a sandbox: it is easy to evade on purpose, which is why the user always sees the exact
  command. Real protection is the confirmation.
- `CLAUDE.md` is the renamed build prompt (the later "vex-starter" version was never in the folder).
