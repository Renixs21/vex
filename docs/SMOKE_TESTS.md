# Manual smoke tests for the tools that were only tested with mocks

Run these once on your own machine. Start the app with `.\.venv\Scripts\python.exe main.py` (tray icon; press
**Win+Alt+V**, tray > Listen now, or click the pill, then type the sentence in the prompt). Or use the terminal chat with
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
- **Hotkey:** Win+Alt+V shows the typing prompt and the pill says "Listening". Press it again while Vex is replying: the voice bar drops
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
