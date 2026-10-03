# Vex UI — desktop orb visual (Windows)

A native desktop app (Python + Qt + a GPU shader, no browser/HTML): a small marbled sphere with
slow, milky liquid swirls and a soft light from the lower left (in the style of the assistant-ui Orb).
Colours: `blue` (default), `default` (grey), `violet`, `emerald`.

## Two modes
| Mode | What you get | Launch |
|---|---|---|
| **Overlay** (default) | The orb floats over your desktop, bottom-centre above the taskbar, with a faint glow. No box, always on top, no taskbar button. Empty space is click-through. | `run_vex_ui.bat` |
| **Window** | Widescreen 16:9 window, dark background. Double-click or F11 for fullscreen. | `run_vex_window.bat` |

## States
`wake` (pops in) · `idle` (dim, slow swirl) · `listening` (brighter, glow, pulses with mic) ·
`thinking` (faster, swirling flow) · `speaking` (brightest; swells, ripples and glows with the voice) ·
`sleep` (fades away completely until the next wake)

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
Raw protocol: JSON to `udp://127.0.0.1:47474`, e.g. `{"state":"speaking","caption":"…","level":0.6}`,
`{"palette":"violet"}`, `{"wake":true}`, `{"quit":true}`.

To start it with Windows, put a shortcut to `run_vex_ui.bat` (with `--start-asleep`) in `shell:startup`.

`python preview.py` / `python preview.py --window` re-render the preview videos (needs ffmpeg).
