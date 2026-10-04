# Integration notes (for Claude Code, wiring this into Vex)

`vex_ui` is only the visual. It runs as its own process and Vex talks to it over UDP. Don't merge
the code into Vex; start it as a subprocess and use the client.

## Start it
`python -m vex_ui.app` (dock mode). Typical for Vex: `pythonw -m vex_ui.app --autosleep 20`.
It starts parked as the edge pill. Requirements: `pip install -r requirements.txt`.

## Hook-up (the only integration points)
```python
from vex_ui.client import VexUI, listen_events
ui = VexUI()
listen_events(on_ui_event)        # {"event": "wake_clicked"} -> start listening like after "Hey Vex"

# wake word heard        -> ui.wake(); ui.listening("Listening…")
# speech captured        -> ui.thinking("Thinking…")
# reply text ready       -> ui.speak_file("reply.mp3", caption=reply_text)   # gTTS mp3; plays + animates, then idle
# user says "sleep"      -> ui.sleep()            # shrinks back to the edge pill
# change colour          -> ui.palette("violet")  # blue | default | violet | emerald
# on Vex shutdown        -> ui.quit()
```
If Vex plays audio itself instead of `speak_file`, send `ui.speaking(text)`, then `ui.level(0..1)`
repeatedly (about 30 times a second) from the audio, then `ui.idle()`. Without levels, the orb
uses a built-in fake speech rhythm.

## Where things are
- `vex_ui/dock.py` – the pill/orb shape, shader, spring and states (no Qt).
- `vex_ui/dock_app.py` – the transparent click-through window, mask, mouse, caption, tray menu.
- `vex_ui/app.py` – entry point, options, UDP control server, mic level.
- `vex_ui/client.py` – what Vex imports.
- `vex_ui/renderer.py` – the older overlay/window orb (and the colour palettes both modes use).

## Tuning
All in `DockConfig` / CLI flags: `--speed`, `--edge`, `--edge-y`, `--center-y`, `--orb-radius`.
Spring constants are in `DockRenderer.update` (`K`, `C`).

## Known: not tested on Windows yet
Built and tested headless on Linux (the real window code was driven offscreen and recorded).
Not yet verified on a real Windows desktop:
- the click-through window mask (`setMask`), always-on-top, and no taskbar button
- the 1-pixel-short window height that stops Windows treating it as a fullscreen app
- performance of the full-screen transparent window at 60 fps (the code only redraws the area
  around the shape; if it is heavy, lower `--fps` to 30)
- high-DPI scaling and multiple monitors (`--screen N`)
