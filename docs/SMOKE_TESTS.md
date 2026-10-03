# Manual smoke tests for the tools that were only tested with mocks

Run these once on your own machine. Start the app with `.\.venv\Scripts\python.exe main.py` (tray icon; press
**Ctrl+Alt+V**, tray > Listen now, or click the pill, then type the sentence in the prompt). Or use the terminal chat with
`.\.venv\Scripts\python.exe main.py --text`: same tools, same safety, confirmations are asked in the terminal.

Have a terminal open with `shutdown /a` typed (not run) before step 5.

| # | Say / type | Expect | Then |
|---|---|---|---|
| 1 | "set the volume to 20 percent" | No dialog (green). The Windows volume slider is at 20. | "set the volume to <what it was>". Then "mute" (icon shows muted) and "unmute". |
| 2 | "set the brightness to 40" | Laptop screen dims. If Windows moves it back a second later, Vex now says so (turn off Settings > System > Display > Brightness > "Change brightness automatically" and the battery-optimising option to keep a manual level). On a desktop monitor you may get "This display doesn't allow brightness control": that is also a pass. | "set the brightness to <what it was>". |
| 3 | "lock my pc" | Yellow dialog "Lock the PC": press **Esc** first: nothing happens. Ask again, press **Enter**: the PC locks. | Unlock with your PIN. |
| 4 | "run powershell as administrator: ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)" | 1) red dialog showing that exact command and "ADMINISTRATOR": click **Approve**. 2) a second dialog "Run this as administrator?": click **Approve**. 3) Windows' own UAC prompt: click **Yes**. Vex reports `True`. | Repeat once and click **No** on the UAC prompt: Vex says the prompt was cancelled and nothing ran. |
| 5 | "shut down the pc" | Red dialog "Shut down the PC in 60 seconds (cancellable)": click **Approve**. Windows shows its shutdown notice. **Immediately** say "cancel the shutdown": no dialog (green), Vex says it's cancelled, the notice goes away. | If anything looks wrong, run `shutdown /a` in your terminal. Optional: same with "restart the pc". |

Quick UI checks while you are at it
- **Hotkey:** Ctrl+Alt+V shows the typing prompt and the pill says "Listening". Press it again while Vex is replying: the voice bar drops
  to zero and a new prompt opens (barge-in).
- **Red dialogs:** pressing Enter right after one appears does nothing; Esc denies; Approve needs a click. For a destructive command, e.g.
  "run powershell: bcdedit /enum", approve the first dialog, then check the CONFIRM dialog: **Approve** stays disabled until you type
  `CONFIRM` exactly. Press **Esc** there so nothing runs.
- **Tray:** Show Vex hides/shows the pill; Mute microphone then Listen now says the microphone is muted; Dry run then "create a file x.txt on my
  desktop" reports "[dry run] would run..." and creates nothing; Open logs opens `logs\`; Quit closes everything.
- **Audit:** open `logs\actions.jsonl`: every step above is there (approved, denied, blocked...), with no memory text.

If a step fails, tell me which number and what you saw (and the last lines of `logs\vex.log`).

## Administrator rights: new checks (for you to run; Claude does not run UAC steps)

Admin is only used when your own message asks for it, or when you say yes to "Retry as administrator?".

| # | Say / type | Expect |
|---|---|---|
| A1 | "run powershell: Get-Date" | A red dialog **without** the word ADMINISTRATOR. No elevation dialog, no UAC. |
| A2 | "run powershell as administrator: Get-Date" | Command dialog (says ADMINISTRATOR), then "Run this as administrator?", then UAC. |
| A3 | "run powershell: Get-ChildItem C:\Windows\System32\config" | Access denied; Vex says so and asks "Retry as administrator?" with no dialog yet. Answer **"yes"**: command dialog, admin dialog, UAC. |
| A4 | Repeat A3 but answer "no", or ask something else | Nothing elevated; asking "yes" two messages later does nothing. |
| A5 | UAC negative test: start A2 and leave the UAC prompt unanswered for ~30 s (or click No) | Vex reports it was cancelled / timed out; nothing ran as admin. This is the proof that nothing auto-approves UAC. |

## Results of the first run (done by Claude on this PC, 2026-10-03)
See the report in the chat; failures found and fixed: elevated command output was unreadable by the normal token (fixed), output BOM (fixed), brightness reverted by adaptive brightness (now reported).


## Voice: for you to run (needs your voice; Claude can't test these)

Claude verified that the real microphone opens, real edge-tts speaks with level updates (~29/s) and real Groq Whisper transcribes.
The laptop mic did not pick up its own speakers (echo cancellation), so the voice loop needs you.

| # | Do | Expect |
|---|---|---|
| V0 | `python tools_dev/mic_check.py --transcribe`, say "open notepad" | Levels move; "Captured ~1.5 s"; Whisper prints "Open Notepad." |
| V1 | Ctrl+Alt+V, say "what's the time" | Pill: Listening, Thinking, Speaking; you hear the answer. |
| V2 | While it is replying, press Ctrl+Alt+V | Speech stops at once; the pill listens again. |
| V3 | Ask something long ("explain how a car engine works") | Two sentences spoken, then "The rest is on screen..."; say "read the rest" and the remainder is read. |
| V4 | Say "lock my pc" (yellow). **This locks the PC.** | Dialog plus spoken "Lock the PC. Should I go ahead?" Say "maybe": counts as no, dialog closes. Ask again, say "yes": it locks. |
| V5 | Say "delete <a test file on the Desktop>" (red) | Vex says "I need your approval on screen"; saying "yes" does nothing; a click approves. |
| V6 | Tray > Mute microphone, press the hotkey | "The microphone is muted." Tray > Type a command still works. |
| V7 | Unplug or disable the mic and press the hotkey | A tray notice explains it and the typed prompt opens. |

## Wake word (needs your voice)
| # | Do | Expect |
|---|---|---|
| W1 | `python main.py --console`; say "Hey Jarvis", then "what's the time" | blip, orb listening, correct time spoken. Log: `wake detected ... score`, `trigger from wakeword -> started`, `wake word resumed` |
| W2 | Say "Hey Jarvis, open notepad" in one breath | Notepad opens (the words after the phrase are kept) |
| W3 | Ask for a long answer ("tell me about Mars") | Vex never starts a new turn while it is speaking |
| W4 | Tray > Wake word off, say "Hey Jarvis" | nothing; the tray line says `Wake word: off`. Turn it on again |
| W5 | Unplug/disable the mic | one notification "Wake word unavailable. The hotkey still works."; hotkey still works; it recovers when the mic is back |
| W6 | Ten minutes of a YouTube video near the mic | at most one false wake (`wakeword_tune.py --quiet 10` counts them) |
