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
