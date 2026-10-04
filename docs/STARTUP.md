# Starting with Windows, and staying up

## Autostart
Vex starts when you log in: a per-user entry in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (no admin, nothing system-wide).
It runs `main.py --supervise --startup-delay 15 --quiet`: it waits 15 s for the desktop to settle, then starts Vex **quietly**: tray icon and
the orb parked as the edge pill, no toast, no sound. Vex listens from logon (wake word + hotkey) unless the microphone is muted.

```
python -m ui.autostart --status
python -m ui.autostart --off      # removes the entry completely
python -m ui.autostart --on
```
Or tray > **Start with Windows**.

* The first time Vex runs after this feature exists it turns autostart on, once, with ONE toast ("Vex starts with Windows now").
* Once you turn it **off** (CLI or tray), Vex remembers (`data/autostart.json`) and never turns it on again by itself, however often it is
  updated or restarted. If something else deletes the entry, that is respected as "off" too.
* If you move the Vex folder, the entry is quietly pointed at the new place while autostart is on.

## The tray icon
The orb icon with a **green dot** = listening. A **grey orb with a red bar** = muted. Tray > **Mute mic / pause listening** toggles it:
muted releases the microphone completely (the wake word listener stops, the hotkey says "The microphone is muted"). Hover the icon for the
state. The mute is not remembered across restarts.

## Recovery
| What dies | What happens |
|---|---|
| Vex itself (crash, killed) | the supervisor restarts it after 3 s with a "Vex restarted" toast. At most **3 restarts in 10 minutes**; then it stops and shows a toast with the log paths (`logs\vex.log`, `logs\child_stderr.txt`). |
| Tray > **Quit Vex** (exit code 0) | never restarted. |
| A second Vex while one runs | the second exits (code 3); the supervisor does not treat it as a crash. |
| The orb process | restarted, same rule (3 per 10 minutes); then the built-in pill takes over and Vex says so. Only an orb that Vex started itself is watched. |
| The hotkey thread, the wake word listener | checked every 30 s, restarted, same rule; one message when giving up. Not while muted / switched off. |

Only the autostart entry (or `python main.py --supervise`) runs under the supervisor. A plain `python main.py` is not restarted if it crashes.
Supervisor notes: `logs\supervisor.log`. The app's error output: `logs\child_stderr.txt`.
