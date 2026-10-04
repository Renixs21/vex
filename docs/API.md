# Local API

A websocket inside Vex for companion apps (a dashboard, a phone-side bridge you run yourself, scripts). Code: `ui/api_server.py`
(transport, auth) and `ui/api_core.py` (every command). Try it with `python tools_dev\api_probe.py state`.

```
ws://127.0.0.1:47480/ws
```

## Security
* **127.0.0.1 only.** The server refuses to bind anywhere else. Requests whose `Host` isn't `127.0.0.1`/`localhost` are refused (DNS
  rebinding), and so are browser pages (any `Origin` header) unless listed in `[api] allowed_origins` (empty by default).
* **A token on every connection**: the text in `data\api_token`, 256 random bits, created on Vex's first start. `data\` is gitignored; the
  token is never logged or sent anywhere. Send `Authorization: Bearer <token>`, or make the first message
  `{"type": "auth", "token": "<token>"}` within 5 s. Anything else is closed with code 4401 after a short delay; after 10 bad tries in a
  minute, new connections get HTTP 429 for a minute. Delete `data\api_token` and restart Vex to make a new one.
* **No confirmations through the API.** There is no command to approve anything. Confirmations still happen in Vex (on screen; yellow
  ones also by voice); clients only *see* that a prompt is showing (`confirm` events). A red tool can be switched off from the API, but
  switching it back on shows an on-screen confirmation in Vex (a voice "yes" doesn't count).
* **Vex behaves the same with no client connected**: the server runs on its own thread; with nobody connected, events are only kept in a
  200-item in-memory activity feed. If the port is taken, Vex starts without the API and logs why. `[api] enabled = false` turns it off.

## Messages
```
-> {"id": 1, "cmd": "text", "args": {"text": "what's the time"}}
<- {"id": 1, "ok": true, "data": {"result": "started"}}
<- {"id": 2, "ok": false, "error": "text is empty"}
<- {"event": "state", "data": {...}}          pushed whenever something changes
```
On connect you get `{"event": "hello", "data": {"commands": [...], "state": {...}}}`.

### Events
| event | data |
|---|---|
| `state` | `state` (one of `idle`, `listening`, `thinking`, `speaking`, `followup`, `muted`, `asleep`), `muted`, `asleep`, `followup_open`, `followup_enabled`, `current_tool`, `confirm` |
| `tool` | `{tool, tier, running: true}` when a tool starts; `{tool, tier, decision, ok, summary, running: false}` when it ends |
| `confirm` / `confirm_done` | a confirmation is showing in Vex: `{tool, tier, title, detail, on_screen_only}`; then `{..., approved, via}` |
| `activity` | one feed item: `turn` (`said`, `reply`), `tool`, `confirm`, `command`, `memory`, `tools`, `autostart` |

### Commands
| cmd | args | does |
|---|---|---|
| `state` | | the snapshot above |
| `text` | `text` | run a command as if typed (max 2000 characters) |
| `listen` | | start listening, like the hotkey |
| `mute` / `unmute` | (`on`) | mute/pause listening (same as the tray) |
| `sleep` / `wake` / `open_orb` | | park the orb as the pill / bring it back |
| `activity` | `limit` (≤200) | the in-memory feed (this session) |
| `audit` | `limit` (≤500), `tool` | rows from `logs\actions.jsonl` |
| `undo.peek` / `undo` | | what "undo that" would reverse / do it (through the normal safety pipeline, audited) |
| `memory.search` | `query`, `limit` | notes matching |
| `memory.note` | `id` or `name` | one note: facts, links, backlinks |
| `memory.neighbours` | `id` or `name`, `hops` (1-2) | `nodes` (`id`, `title`, `type`, `importance`, `hop`) and `edges` (`source`, `target`) |
| `memory.inbox` / `memory.keep` / `memory.discard` | `path`, `block` (from the inbox), optional `note`, `type` for keep | the curator's proposals |
| `memory.size` | | vault size vs the 200 MB soft cap |
| `tools.list` | | every tool: `tier`, `category`, `enabled`, `description`, `last_used` |
| `tools.disable` / `tools.enable` | `name` | switch a tool off/on (saved in `data\tools_disabled.json`; red: on-screen yes to switch on) |
| `health` | | the free health check (no API quota), keys redacted |
| `latency` | `last` | the latency report summary |
| `autostart.get` / `autostart.set` | `on` | Start with Windows |
| `budget` | | money vs `[budget] budget_inr` (1000): only Google Cloud TTS can cost anything, and it is off by default |
| `commands` | | the list |

A switched-off tool is never offered to the brain and the executor refuses to run it (audited as `disabled`). Nothing switches a tool
back on by itself.

## The probe
```
python tools_dev\api_probe.py state
python tools_dev\api_probe.py text "what's the time"
python tools_dev\api_probe.py watch
python tools_dev\api_probe.py call memory.neighbours "{\"name\": \"Rahul\", \"hops\": 2}"
python tools_dev\api_probe.py call tools.disable "{\"name\": \"run_powershell\"}"
```
