# VexMind: how Vex's memory works

Plain markdown notes in `VexVault/` (people, projects, topics, places, profile, preferences, tasks, daily, inbox) with `[[wikilinks]]`,
indexed in SQLite (`VexVault/.vexmind/index.db`, rebuildable). You can edit any note by hand; the index follows within seconds.

## Saving: explicit and automatic
- **You say "remember ..."**: the agent calls `remember`, which saves immediately (secrets are refused).
- **The curator** (`memory/curator.py`) reads each finished conversation and saves durable facts you stated.
  - A conversation is over after 120 s of quiet. One LLM call (gpt-oss-20b first, then 120b, then Gemini) runs in the background and
    only while Vex is idle; it never delays a reply. Failures keep the conversation and retry later (5 min, 15 min, ...; dropped and logged after 6).
  - Rules in the prompt: never save passwords, keys, card/bank numbers, government IDs, or other people's health, money or private
    life; when unsure skip or lower the confidence. Code backstops: the secret regex filter, and sensitive words about another person.
  - **Inbox instead of notes** when: confidence < 0.7; a personal fact (profile, preference, person, place) < 0.8; sensitive; or a proposed
    deletion (never applied automatically).
  - Every decision is logged to `logs/curator.jsonl`. "Forget ..." removes curator-saved facts like any others.
- Settings: `[memory]` in `config.toml` (`curator`, `curator_idle_seconds`, `curator_order`, ...).

## Nightly tidy-up
`memory/consolidate.py`, about 03:00 (or at the next start if missed), only while idle. Merges near-duplicate facts in a note, promotes
inbox items only if they are project/topic facts proposed twice, compresses daily notes older than 30 days into weekly notes (originals to the
Recycle Bin), and lowers the importance of notes unused for 90 days (at most one point a month). Never deletes a fact; everything is logged in
`logs/memory_consolidation.jsonl`. Preview: `python -m memory consolidate --dry-run`.

## The viewer
Tray > **Open memory**. A graph of your notes (size = number of links, colour = type, dashed = a link to a note that doesn't exist yet), a note
panel with clickable links, backlinks and an **Edit** mode (plain markdown, saved atomically; refuses secrets and a changed id), search, tags,
recent notes, and the **Inbox**: approve a fact (you can correct the note name and type first) or discard it. Proposed deletions can only be
discarded. The page (`ui/memory.html`, graph library vendored in `ui/vendor/`) talks to Python over a QWebChannel bridge with a whitelist of
calls (`ui/memory_viewer.py` -> `memory/viewer_service.py`); there is no network port. It refreshes itself when notes change. Light/dark toggle.

## Command line
`python -m memory remember|recall|forget|read|list|link|rename|stats|reindex|consolidate`.
