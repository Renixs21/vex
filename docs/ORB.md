# The orb

`vex_ui/` is the orb: a native, GPU-rendered marbled sphere (Python + Qt + a shader, no HTML) that floats over the desktop. Vex
starts it automatically and drives it over UDP on localhost. Nothing else needs to be running.

## How it is wired
```
VexController --set_state / set_level / show_caption / show / hide--> MultiFrontend --> OrbFrontend --UDP 47474--> vex_ui.app
                                                                                             clicks <--UDP 47475-- ClickListener
```
- `ui/orb_frontend.py`: `OrbFrontend` (the `VisualFrontend` implementation), `OrbProcess` (starts/stops the orb), `ClickListener`,
  `MultiFrontend`. Sends are fire-and-forget, so a slow or missing orb can't stall Vex.
- States map one to one: idle, listening, thinking, speaking. `hide()` fades the orb away (its "sleep"), `show()` pops it back.
- While you speak, the recorder's level drives the orb; while Vex speaks, the audio player pushes the voice level about 30 times a second.
- Captions (what it heard, then the reply) are shown under the orb, shortened to about 160 characters; the tray's "Show Vex" toggles it.
- **Click the orb to start listening.** (`vex_ui/vex_ui/app.py` got a `--click-port` option for this: a click that doesn't drag sends
  `{"click": true}` to Vex.) Dragging still moves it; right-click gives the orb's own menu.
- Vex only closes an orb it started itself. If you launch `vex_ui` by hand first, Vex just uses it.

## Dock mode (default)
Vex lives as a thin pill on the right screen edge. A wake word, hotkey or state change makes it peel off, swell into the orb and glide
to the middle of the screen; after `orb_autosleep` idle seconds it shrinks back into the edge. **Click the pill** (or the tray icon) to
wake it by hand: the orb sends `{"event": "wake_clicked"}` to the click port and Vex starts listening. `orb_mode = "overlay"` gives the
older floating orb (click it to talk). Clicking an already-awake dock orb does nothing; use the hotkey or wake word.

## How the window works
One small (560x350) transparent window follows the shape, so Windows never sees a fullscreen app and only a small area is composited.
It is click-through (`WS_EX_TRANSPARENT`) except while the cursor is over the pill/orb; a window mask is not used because it would clip
the glow and the caption. `python -m vex_ui.app --debug` prints fps and ms per frame every 2 s (measured here: about 60 fps, 9-13 ms at
200% scaling on Intel Iris Xe). Errors and crashes go to `vex_ui/vex_ui.log`.

## Configure (`[ui]` in config.toml)
| Key | Meaning |
|---|---|
| `visual = "orb"` | `orb`, `pill` (the small built-in Qt widget) or `both` |
| `orb_mode` | `dock` (default) or `overlay` |
| `orb_edge`, `orb_edge_y`, `orb_center_y`, `orb_radius` | dock: edge (right/left), pill height, landing height, orb size |
| `orb_position` (overlay only) | bottom, bottom-right, bottom-left, center, top-right |
| `orb_palette` | blue, default (grey), violet, emerald |
| `orb_size` | e.g. `"420x340"`; blank = default |
| `orb_autosleep` | seconds idle before it fades away (0 = stays) |
| `orb_port`, `orb_click_port` | UDP ports (change them only if something else uses them) |

If the orb can't start (missing folder, GPU driver problem) Vex tells you once and uses the pill instead.
Run `python -m vex_ui.app` from the `vex_ui` folder to see the orb's own error output.

## Dependencies
`moderngl`, `Pillow`, `PySide6`, `numpy` (in requirements.txt). It needs an OpenGL-capable GPU/driver.

## Standalone use
`vex_ui/README.md` describes the orb on its own (window mode, options, the JSON protocol).
