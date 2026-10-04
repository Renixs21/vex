# Housekeeping

Runs by itself 2 minutes after Vex starts, then once a day. By hand:
```
python tools_dev\housekeeping.py --dry-run     # what it would do
python tools_dev\housekeeping.py
```
Everything it archives, rotates or removes is written in plain words to `logs\housekeeping.log`.

| What | Limit | Then |
|---|---|---|
| `logs\actions.jsonl` (the audit log) | 5 MB | appended to `logs\archive\actions-YYYY-MM.jsonl.gz` and started fresh |
| those audit archives | 50 MB in total | the oldest month is removed |
| `curator.jsonl`, `memory_consolidation.jsonl`, `latency.jsonl` | 2 MB each | rotated to `name.1.jsonl.gz` … `.3` (3 kept) |
| stray files in `logs\` (`console*.txt`, `app_stdout*.txt`, `bench_*.json`, `eval_*.json`, `*.old.txt`) | untouched for 30 days | removed |
| leftover `*.tmp` in `logs\`, `data\`, `VexVault\.vexmind\` | untouched for 30 days | removed |
| `data\undo_backups` (copies kept for "undo that") | 50 MB | oldest removed (those old edits can no longer be undone) |
| `VexVault` | **200 MB soft cap** | 80%: a warning (once a day, also in the health check). 100%: automatic memory entries (the curator) pause; conversations stay queued and are processed once there is room. |

Not managed here: `vex.log` (rotates itself at 1 MB x 3), `supervisor.log` (200 KB x 2), `child_stderr.txt` (set aside at 1 MB by the
supervisor), `health.txt` (overwritten each run).

**Your notes are never touched.** Housekeeping never deletes, moves or edits a note, at any age or vault size: the only files it may
remove inside the vault are stray `.tmp` leftovers in the hidden `.vexmind` folder. "remember …" that you ask for still works when the
vault is over the cap; only the automatic entries pause. A busy log (open in another program) is skipped and rotated next time.
