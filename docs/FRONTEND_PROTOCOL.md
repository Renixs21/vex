# Visual frontend protocol

Vex's Python side drives whatever draws Vex through five calls (`ui/frontend.py`, `VisualFrontend`). The temporary Qt pill
implements them directly. A separate app (for example Godot) can implement them over a local socket: every call is one
JSON object per line (UTF-8, `\n` terminated), sent from Python to the frontend. `JsonLinesFrontend(write)` produces exactly
this; `decode()` / `dispatch()` are the receiving side.

| Message | Fields | Meaning |
|---|---|---|
| `{"op":"state","state":"idle"}` | `state`: `idle`, `listening`, `thinking`, `speaking` | What Vex is doing. Treat the level as 0 after a change. |
| `{"op":"level","level":0.42}` | `level`: number 0..1 | Loudness of Vex's voice right now. Sent about **30 times a second** while speaking, 3 decimals, repeats dropped. Smooth it on your side (fast attack, slow release looks good). |
| `{"op":"caption","text":"..."}` | `text`: string | What was heard, or Vex's reply. `""` clears it. |
| `{"op":"show"}` / `{"op":"hide"}` | | Make the visual visible / invisible. Everything else keeps working while hidden. |

Rules
- Python to frontend only. Fire and forget: no replies, no acknowledgements, so a slow or missing frontend never stalls Vex.
- Unknown `op`s must be ignored (so the protocol can grow); malformed lines can be dropped.
- If the connection drops, Python keeps running; on reconnect, expect the next `state` / `show` messages. A frontend that
  connects late should assume `idle`, visible, no caption.
- Confirmation dialogs are **not** part of the protocol and never will be: they stay native Python windows
  (`ui/confirm_dialog.py`), so a frontend can't approve anything.
- Clicking the visual should tell Python to start listening. That is the one thing that goes the other way; the plan is a
  separate `{"op":"click"}` line from the frontend to Python, handled by calling `controller.start_listening("click")`. It is not
  implemented yet because it needs the transport.

Not built yet: the socket transport itself (server side in Python, ~30 lines on top of `JsonLinesFrontend`).
