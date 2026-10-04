# Vex UI — desktop orb visual (Windows)

A native desktop app (Python + Qt + a GPU shader, no browser/HTML): a small marbled sphere with
slow, milky liquid swirls and a soft light from the lower left (in the style of the assistant-ui Orb).
Colours: `blue` (default), `default` (grey), `violet`, `emerald`.

## Modes
| Mode | What you get | Launch |
|---|---|---|
| **Dock** (default) | Vex lives as a thin pill on the right screen edge (like Samsung's Edge Panel handle). "Hey Vex": it peels off, swells into the orb and glides to the middle of the screen. Sleep: it shrinks back into the edge. Click the pill to wake it by hand. | `run_vex_ui.bat` |
| **Overlay** | The orb floats at the bottom-centre of the desktop, transparent and click-through, and fades out when asleep. | `python -m vex_ui.app --mode overlay` |
| **Window** | Widescreen 16:9 window, dark background. F11 / double-click for fullscreen. | `run_vex_window.bat` |

Dock options: `--edge right|left`, `--edge-y 0.40` (pill height, fraction from the top),
`--center-y 0.50` (where the orb lands), `--orb-radius 62`, `--speed 1.0` (about a 1 s move;
0.7 slower, 1.3 faster), `--screen 0`, `--start-awake`.

## Install
```
pip install -r requirements.txt
pip install sounddevice pygame pydub     # optional: mic reaction, speak_file(), real voice loudness
run_vex_ui.bat
```
Options: `--mode overlay|window`, `--position bottom|bottom-right|bottom-left|center|top-right`,
`--palette blue|default|violet|emerald`, `--size 420x340`, `--fullscreen`, `--autosleep 20`, `--start-asleep`, `--mic`.

Controls: drag the orb to move it · right-click it or use the **tray icon** for the menu
(states, Colour, Wake, Recentre, Quit) · keys `1–5` states, `W` wake, `Esc` sleep (overlay) / quit (window).

## Driving it from Vex's backend
Start the UI once (e.g. `--start-asleep --autosleep 20`), then from the backend:

```python
from vex_ui.client import VexUI
ui = VexUI()

ui.wake()                                        # "Hey Vex" detected -> orb pops in
ui.listening("Listening…")
ui.thinking("Thinking…")
ui.speak_file("reply.mp3", caption=reply_text)   # plays the gTTS audio + animates, then idle
ui.sleep()                                       # or let --autosleep do it
```
Events back from the UI (when the user wakes Vex by clicking the pill or tray icon):
```python
from vex_ui.client import listen_events
listen_events(lambda e: start_listening() if e["event"] == "wake_clicked" else None)   # udp 47475
```
Raw protocol: JSON to `udp://127.0.0.1:47474`, e.g. `{"state":"speaking","caption":"…","level":0.6}`,
`{"palette":"violet"}`, `{"wake":true}`, `{"quit":true}`.

To start it with Windows, put a shortcut to `run_vex_ui.bat` (with `--start-asleep`) in `shell:startup`.

`python preview_dock.py` records the real dock window code over a mock desktop (needs ffmpeg).
`python preview.py` / `python preview.py --window` render the older overlay/window previews.
