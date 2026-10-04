# Vex Control Center

A desktop app (PySide6 + QML, in `vex_app/`) for seeing and steering Vex. It talks **only** to Vex's local API
(`ws://127.0.0.1:47480`, token from `data\api_token`; see `docs/API.md`). It never imports Vex's own modules (a test enforces this),
and it can't approve anything: confirmations happen on Vex's own screen.

## Opening it
* Tray > **Open Control Center**
* Double-click the edge pill (the first click's listening turn is cancelled)
* `python -m vex_app` (`--screen tools` to open on a screen)

One instance only: a second launch brings the open window to the front. Closing the window never stops Vex. If Vex isn't running the
window says so, offers **Start Vex** (starts the supervisor quietly), and reconnects by itself every few seconds.

## Screens
| Screen | Status |
|---|---|
| Home | built |
| Memory | built |
| Tools & permissions | built |
| Conversations | built |
| Tasks & reminders | built |
| Voice | built |
| System & health | built |
| Budget | next |

### Home
* **Header:** a status chip ("Listening for 'Hey Vex'", "Still listening", "Microphone muted"...), the follow-up window length, and
  **Mute mic**.
* **The orb:** the edge orb's marbled liquid metal (a Qt ShaderEffect, `vex_app/shaders/orb.frag`), breathing with Vex's state: idle,
  listening, thinking (swirls faster), speaking (pulses), follow-up (calmer listening), muted / asleep (grey and slow).
* A greeting for the time of day, then "Say 'Hey Vex', press Ctrl+Alt+V, or type below". The input bar: mic (listen now), a text field,
  and the chrome **Send**. Quick chips fill in the start of a command (Set a reminder, Open an app, Search the web, Remember something).
* **Right column:** Today (reminders due today or overdue) and Activity (the last 10: time and tool name in mono, a tier-colour dot,
  what happened; filled from the audit log when Vex has just started). "Audit log" goes to Tools & permissions.
* **Tiles:** response time (median "you stopped talking -> first word", first audio, turns counted), health (the last check; **Run check**
  runs the free one, about 5 s), budget (rupees used of 1000, with a bar).
* **A confirmation in progress** shows as a banner: "Vex is asking: ... | Confirm on Vex's screen". There is no approve button.

### Memory
Tabs **Focus | Galaxy | Timeline | Notes** and a search box (results drop down; Enter or click opens the note).
* **Focus** (default, opens on your best-connected note): a sigma.js graph in a QtWebEngine panel (all scripts bundled in
  `vex_app/web/vendor/`, MIT; no CDN). The selected note sits in the middle, its neighbours on a ring, theirs on an outer ring. Colour by
  type: Projects `#93a8bf`, People `#cdb98a`, Preferences `#c8896a`, everything else (facts) `#b9bec6`; size by importance. Edges are
  faint `#3a3e45`; 2nd-hop edges are dashed and fainter. Hovering a node lights only its edges; clicking re-centres on it. Legend bottom
  left, zoom +/- bottom right.
* **Galaxy:** every note (daily logs included: they hold many links), laid out with ForceAtlas2; small labels appear as you zoom in.
* **Timeline:** what Vex learned, grouped by day, with the note and the source of each fact.
* **Notes:** a card per note (type, title, facts, last change), filtered by the search box.
* **Right panel:** the selected note: type pill, title, text, its `[[links]]` as chips (click to go there), "What Vex knows" (each fact
  with when it was learned and where from), "Mentioned in", and **Open** (in your editor) / **Edit** (in the app; secrets and a changed id
  line are refused, like Vex's own viewer) / **Forget...** (shows exactly what Vex would remove, asks, then moves the note to the Recycle
  Bin). Below: the **Inbox** (facts Vex wasn't sure about) with Keep / Discard.
* The sidebar card shows the vault size against its 200 MB soft cap while you're on Memory.

### Tools & permissions
* Tier tabs **All | Green | Yellow | Red** (with counts) and a search box.
* The table: **Tool** (mono) | **What it does** | **Tier** pill | **Last used** (from the audit log) | **On** (a machined switch).
  Switching a tool off takes effect at once: Vex stops offering it to the brain and refuses to run it. Switching a **red** tool back on
  shows "Confirm on Vex's screen" here while Vex asks in its own on-screen dialog (never by voice); nothing switches a tool back on by
  itself.
* **Audit log** (right): every tool call, newest first: time, tool, what happened and how it was approved. **Undo** (or **Restore** for
  a deleted file) reverses the one action "undo that" would reverse, through Vex's normal safety pipeline.
* **How tiers work**: green runs at once; yellow asks first (voice, hotkey or screen); red always shows the exact command or path on
  Vex's screen, never by voice and never from this app.

### Conversations
Sessions on the left (one wake-up and its follow-ups; newest first, with turns, median response time and the tools used), the transcript
of the chosen one on the right: what you said, the tools Vex used on the way (tier dot, mono; a declined one says so), the reply, and
the turn's timing (total, speech recognition, brain, first sound).

**What is kept:** your words and Vex's replies, in memory and in `data\conversations.jsonl` (gitignored), for
`[api] keep_conversations_days` (14; `0` keeps them only until Vex stops). Passwords, keys, card numbers and the like are redacted before
anything is stored. Older sessions are dropped when Vex starts and once a day. Before this, Vex kept only daily summaries.

### Tasks & reminders
* **Tasks** (`VexVault/tasks.md`, which you can also edit by hand): add one (with an optional due date in plain words: "tomorrow",
  "Friday 5pm"), tick it off, or delete it. Overdue dates are bold; done tasks sink to the bottom, struck through.
* **Reminders**: what and when ("in 20 minutes", "tomorrow 9am", "every weekday at 9:30"), each with its next time and repeat rule;
  Cancel removes one.
* Every change goes through Vex's own tools, exactly as if you'd said it: audited, and deleting a task asks first on Vex's screen.

### Voice
* **Wake word:** a live microphone meter and the "Hey Vex" score against the threshold line (say it and watch it cross), and a
  sensitivity slider (right = wakes more easily; it sets `[wakeword] threshold`, 0.05-0.95).
* **Follow-up:** on/off and the window length (2-30 s).
* **Vex's voice:** the edge-tts voices from `[voice] edge_candidates`; **Preview** says a sample sentence (only while Vex is idle) without
  changing anything; picking one switches Vex's voice at once.
* Every change takes effect immediately and is saved to `config.toml`. Out-of-range values and unlisted voices are refused.
* **Response time per voice turn:** the last 40 turns, one column each (you stopped talking -> first word), with the median line; hover
  (or focus the chart and use the arrow keys) for a turn's time, how it started and its stages; **Table** shows every number as text.
  (Turns before 2026-10-04 23:00 still include time spent answering confirmations; newer ones don't.)

### System & health
* **Health:** the free check (`docs/HEALTH.md`): every part with PASS / WARN / FAIL (a word, not just a colour), the detail and what to
  do. **Run check** takes about 5 s and uses no API quota.
* **Vex:** version (git commit), Python, pid, and whether the supervisor keeps it running; **Start with Windows**; **Restart Vex**
  (asks first). Under the supervisor Vex exits with code 75 and is started again at once; that is not counted as a crash and shows no
  "restarted" toast. Started by hand, the Control Center starts it again itself if it isn't back after 12 s.
* **Logs:** the last 400 lines of `vex.log`, `supervisor.log`, `housekeeping.log`, the error output and the last health report, with keys
  and secrets hidden; **Open logs folder**.

## Look
Base `#0b0c0e`. Panels: brushed gunmetal (`shaders/panel.frag`: gradient `#1b1d21`->`#141619`, very faint vertical brush lines, 1px
outline `#262a30`, 1px top highlight at 7% white, radius 16). Text `#e6e9ed`, secondary `#a0a6af`, labels `#8d939c` (uppercase,
tracked 0.14em, 13 px). Fonts: Geist and Geist Mono (bundled from Google Fonts, SIL OFL: `vex_app/fonts/OFL.txt`); numbers, times and
tool names are mono. Buttons are machined (`#2a2e34`->`#1e2126`, top highlight, outline `#33373e`); the primary button is chrome
(`#f1f3f6`->`#b8bec7`, dark text). The only colours: the orb, the status dot `#6fcf97`, and tier pills (green `#6fcf97`, yellow `#e2c26b`,
red `#e2776b`: tinted background, 1px ring, light text). Motion 180-240 ms ease-out, no bounce. Every control is at least 44 px, reachable
with Tab, and shows a focus ring. The sidebar collapses to icons under 1100 px; the window resizes freely (minimum 900x640).

## For developers
```
python -m vex_app --screenshot tools_dev/screens/home_1280x800.png --size 1280x800 --screen home   # save a PNG and quit
python tools_dev/build_shaders.py      # after editing vex_app/shaders/*.frag (compiles .qsb with Qt's qsb)
```
`vex_app/client.py` is the API client, `vex_app/viewmodels.py` turns replies and events into properties the QML binds to,
`vex_app/qml/` holds the screens and components (`components/Theme.qml` is the whole palette). Tests: `tests/test_vex_app.py`
(pytest-qt).
