# What Vex can do on the PC, and how each action is gated

Every tool call goes through `safety/executor.py`: classify, confirm, run, scrub, log. Each call is appended to `logs/actions.jsonl`
(time, tool, tier, decision, arguments, result, and for confirmed actions `via`: which channel approved it: `voice`, `dialog`, `typed`,
`hotkey` or `tray`). Memory text is the only thing left out of the arguments.

**Tiers.** green: runs. yellow: Vex asks ("Should I ...?"); a spoken "yes" or "no", a click, or Enter works. red: asks, shows the exact
command or path, and accepts only an on-screen answer (click or typing), never speech; dangerous commands also need CONFIRM typed.
blocked: never, nothing to approve.

## Files
| Tool | Tier | Notes |
|---|---|---|
| `create_file` | green in safe folders | If the file exists: yellow, "Replace it (No saves a copy as `name (2).txt`)". Yes replaces it and keeps the old version for undo; No saves the copy; no answer does nothing. |
| `append_to_file`, `replace_in_file`, `overwrite_file` | yellow, always | Show a diff of the change first. UTF-8 text only (no binaries, max 1 MB). `replace_in_file` needs a unique match (or `replace_all`). Protected places, secret files (`.env`, keys, `.ssh`) and system folders are blocked outright. |
| `read_file`, `list_dir`, `copy_file`, `move_file` | green in safe folders | outside them yellow; protected places red or blocked |
| `delete_file` | red | Recycle Bin, exact path shown |
| `undo_last` | green | Undoes create, edit (restores the earlier text exactly, refuses if the file changed again since), copy, move, delete. Last 20 actions, survives restarts. |

## Apps, links, windows, typing
| Tool | Tier | Notes |
|---|---|---|
| `open_app` | green; yellow for shells and admin tools | Command prompt, PowerShell, Windows Terminal, regedit, mmc, services, diskpart, wsl and similar (also when a Start Menu shortcut resolves to one) need a yes. |
| `open_url` | green if you named the site, else yellow with the URL shown | http and https only; no `file:`, `javascript:`, `data:`, no credentials in the address; local network addresses always ask. |
| `list_windows`, `focus_window`, `minimize_window`, `maximize_window`, `restore_window`, `snap_window`, `show_desktop` | green | Windows are matched by exact title, start of title, text in the title or the app name; no fuzzy guessing. |
| `close_window` | yellow | Asks the app to close (it can still ask "save?"). |
| `type_text`, `press_keys` | yellow; red (on-screen only) after reading outside content | The dialog shows the exact text or keys and the window. See below. |
| `close_app`, `install_app`, `uninstall_app`, `lock_pc`, `forget` | yellow | |
| `run_powershell`, `shutdown_pc`, `restart_pc` | red | exact command shown |

### Typing and keys
- **"Open Notepad and write a note: ..."** works as `open_app`, then `type_text` with the window name. The text goes to the window that
  `open_app` just opened, never to an older window of the same app (which may hold your own text). If the app opened no new window,
  nothing is typed and Vex says so.
- If several windows match, the confirmation says "N windows match, using the front-most one".
- Typing or keys into a command window (cmd, PowerShell, Windows Terminal), the Run box or a registry editor are refused, and so are
  Win+R and Win+X: they would run commands without the exact-command confirmation that `run_powershell` gives.
- Limits: 1,000 characters per `type_text`; up to 6 key combinations per `press_keys`.

### Untrusted content
Text that comes back from `read_file`, `list_dir`, `web_search`, `web_fetch`, `get_clipboard`, `play_youtube`, `list_windows` is data, never
instructions. Once any of them has returned something during a turn, `type_text` and `press_keys` for the rest of that turn become red:
on-screen approval only, with a warning that the text may not be yours. The system prompt also tells the model to ignore instructions
in such text. The user's own message is the only trusted instruction.

## Testing safely
Tests never touch the real desktop: `tests/conftest.py` installs a backend that raises if the real window code is reached, and the tests use
`tests/fake_windows.py`. To try the real thing, point it at a throwaway window you created yourself, never at your own documents.

## Reminders and tasks
| Tool | Tier | Notes |
|---|---|---|
| `set_reminder` | green | One-time ("in 20 minutes", "tomorrow 5pm") or recurring: "every weekday at 9", "every Monday and Thursday at 6:30pm", "daily at 8", "every 2 hours" (not below 5 minutes), "every other day at 8am", "every month on the 5th". A missing day or time is asked, not guessed. A bare "at 9" means 9 AM, "at 5" means 5 PM. |
| `list_reminders` | green | Numbered in the order they come due, recurring ones with their rule and next time. |
| `cancel_reminder` | green (cancel all: yellow) | By number or words from its text; an ambiguous match lists the candidates and asks for the number. |
| `add_task`, `list_tasks`, `complete_task` | green | The list is `VexVault/tasks.md` (a plain markdown checklist you can edit by hand). A due date such as "friday at 5pm" is stored as a real date. |
| `edit_task` | green | Change the text and/or due date (`due: none` clears it); keeps the checkbox. |
| `delete_task` | yellow | Shows the exact task. `undo_last` puts it back in the same place. |
| `today_summary` | green | "What's on today": reminders due today, tasks due today, overdue tasks, and how many other tasks are open. |

**When a reminder fires:** Vex plays a two-note chime, shows the words on the orb and says "Reminder: ..." aloud, with a silent toast as well.
If you are in the middle of a conversation it waits until that finishes (and the wake word stays paused while it speaks). If speaking fails
the normal toast with sound is used instead. In text mode (no orb, no voice) the toast is the notification.

**Surviving restarts:** reminders live in `data/reminders.json`. One-time reminders that came due while Vex was off are announced at the next
start as "Reminder from earlier: ..."; a recurring one that missed an occurrence announces it once and carries on. A sweep every minute also
catches anything a sleeping PC skipped. Cancelled and finished reminders are kept for 14 days and then dropped.

## The web
| Tool | Tier | Notes |
|---|---|---|
| `web_search` | green | DuckDuckGo with one quick retry, then Bing, then Tavily (only if `TAVILY_API_KEY` is in `.env`; free tier), then Wikipedia. An empty result moves to the next engine; if every engine is unreachable Vex says so instead of "no results". The same search is remembered for 5 minutes. |
| `quick_facts` | green | Wikipedia's short summary of a topic, person or place; says so when the name is ambiguous. |
| `get_weather` | green | Open-Meteo (free, no key): now, today and the next two days. `weather_place` in `[web]` is the default city, `units` is metric or imperial. Without a default Vex asks which city. |
| `web_fetch` | green if you named the page or `web_search` returned it this turn, otherwise yellow with the URL shown | Reads one page as plain text (`question` pulls out the parts that answer it; up to 8,000 characters). |

**What `web_fetch` refuses:** anything but http/https; addresses with a username or password or with something that looks like a secret in them; local
and private addresses (`localhost`, `192.168.x.x`, `10.x`, `169.254.x.x`, names that resolve to them), checked again after every redirect (up to 4);
files that aren't text (images, zips, PDFs); downloads over 2 MB are cut. The page cannot reach back: scripts, forms and navigation are dropped.

**Web text is data, never instructions.** Pages, search results, Wikipedia and weather text are labelled "UNTRUSTED", sentences that read like
orders to an AI ("ignore previous instructions", "run PowerShell...") are removed from page text and counted in the result, the model is told to
summarise and never obey, and after any web content has come back the rest of that turn is "tainted": typing, key presses and unexpected links need
an on-screen yes with a warning. Even a model that obeyed a page would still hit the confirmations for PowerShell, deleting, typing and so on.
One honest limit: the address is checked just before the request, so a site that changes where its name points in between could slip a local address
past that check; reading a page never changes anything on your PC, and the answer only comes back to the model.
