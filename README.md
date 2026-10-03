# VEX


## Memory (VexMind)

Vex's memory is built in: plain markdown notes in `VexVault/` with `[[wikilinks]]`, backlinks and tags, indexed in
SQLite (`VexVault/.vexmind/index.db`, always rebuildable). No Obsidian needed. Spec: `MEMORY_PROMPT.md`.

    python -m memory remember "Rahul is my roommate" --about Rahul --type person --alias "my roommate"
    python -m memory recall "who is my roommate"
    python -m memory forget "Rahul"            # shows what it would delete, asks first (--yes to skip)
    python -m memory reindex                   # rebuild the index from the files
    python -m memory stats | list | read <title> | link <a> <b> | rename <old> <new>
    python -m pytest tests                     # all tests
    python tools_dev/memory_bench.py           # retrieval timing on a generated 1,000-note vault

## Vendored libraries

- `ui/vendor/force-graph.min.js`: [force-graph](https://github.com/vasturiano/force-graph) **1.52.0** (MIT, license in
  `ui/vendor/force-graph.LICENSE`). Used by the memory viewer; loaded from disk, never from a CDN.

## Running Vex

    .\.venv\Scripts\python.exe main.py              # tray icon + floating pill + Ctrl+Alt+V (type instead of speak for now)
    .\.venv\Scripts\python.exe main.py --text       # terminal chat
    ... --dry-run                                     # tools only report what they would do (also a tray toggle)

Approvals for yellow and red actions appear as native dialogs. Manual checks for the hardware-touching tools:
`docs/SMOKE_TESTS.md`. The visual is behind a small interface (`docs/FRONTEND_PROTOCOL.md`).

The visual is the orb in `vex_ui/` (see `docs/ORB.md`); voice is described in `docs/VOICE.md`.

Say "Hey Jarvis" (the built-in fallback) or, once you've trained it, "Hey Vex": see `docs/WAKEWORD.md`. Run `python main.py --console` to watch every stage in the terminal.
