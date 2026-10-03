# Task: VexMind — Vex's own linked-notes memory

Build **VexMind**, Vex's long-term memory. It works like Obsidian (plain markdown notes, `[[wikilinks]]`, backlinks, tags, daily notes, a graph view) but is **built into Vex** — no Obsidian install or plugin needed.

Read `CLAUDE.md` first for the overall project. Show me your plan (modules, schema, libraries) before writing code, and ask if anything conflicts with what's already built.

---

## 1. Design principles

1. **Markdown files are the source of truth.** Everything Vex knows lives in `.md` files I can read and edit by hand. The database is only an index and can always be rebuilt from the files.
2. **Everything is linked.** Notes connect through `[[wikilinks]]`. Retrieval follows links, so asking about one thing surfaces related things.
3. **Facts have provenance.** Every fact records when it was learned and from which conversation.
4. **Local and private.** Memory stays on my PC. Only the few notes relevant to a question are sent to the LLM.
5. **Vex remembers deliberately.** It saves durable facts (people, projects, preferences, plans), not chit-chat.

## 2. Storage: the vault

```
VexVault/
├── people/        rahul.md, mom.md
├── projects/      portfolio-site.md, vex.md
├── topics/        fitness.md, food.md
├── places/
├── preferences.md
├── profile.md
├── tasks.md
├── daily/         2026-10-03.md       # one per day: what happened, conversation summaries
├── inbox/         unsorted facts waiting for consolidation
└── .vexmind/      index.db, settings.json   # hidden, rebuildable
```

Each note:

```markdown
---
id: 01JABC...            # ULID, never changes even if the file is renamed
type: person             # person | project | topic | place | preference | profile | daily | task-list
title: Rahul
aliases: [Rahul K, my roommate]
tags: [friend, college]
created: 2026-10-03T19:50:00+05:30
updated: 2026-10-03T19:50:00+05:30
importance: 3            # 1-5, raised when I reference it often or say it matters
---

# Rahul

- Roommate since 2024, studies [[mechanical-engineering]] #college  ^f1
  <!-- src: daily/2026-10-03 · learned: 2026-10-03 -->
- Working with me on [[portfolio-site]]  ^f2
```

- Each fact is one bullet with a stable block ID (`^f1`) so it can be updated or deleted precisely.
- Provenance goes in an HTML comment under the bullet (invisible when rendered).
- Links can target a note by filename or any alias. Unresolved links are allowed (they show as "ghost" nodes, like Obsidian).
- `#tags` inline or in frontmatter both count.

## 3. Index: SQLite

`memory/index.py` maintains `VexVault/.vexmind/index.db` (Python's built-in `sqlite3`, FTS5):

| Table | Purpose |
|---|---|
| `notes(id, path, title, type, importance, created, updated, mtime, hash)` | one row per note |
| `aliases(alias_norm, note_id)` | name resolution ("my roommate" → Rahul) |
| `blocks(note_id, block_id, text, learned, source)` | one row per fact bullet |
| `links(src_note, src_block, target_note, target_raw)` | the graph; `target_note` is NULL for ghost links |
| `tags(note_id, tag)` | tag lookup |
| `blocks_fts` | FTS5 virtual table over block text + note titles, BM25 ranking |
| `access(note_id, last_used, use_count)` | for recency and importance boosts |

- **Incremental sync:** on start, compare file `mtime`/hash and re-index only changed files. A `watchdog` file watcher re-indexes when I edit a note by hand.
- `vex memory --reindex` rebuilds everything from scratch.
- Renaming a note updates `[[links]]` pointing to it across the vault.

## 4. Retrieval (recall)

`memory/retrieve.py` — given the current user message, return a small context pack (cap ~1,500 tokens) to inject into the LLM prompt:

1. **Entity match:** find note titles/aliases mentioned in the message (fuzzy with `rapidfuzz`).
2. **Full-text search:** FTS5 BM25 over fact blocks.
3. **Graph expansion:** for the top notes, pull 1-hop neighbors (outgoing links + backlinks), scored lower.
4. **Always-on context:** `profile.md` and `preferences.md` (short).
5. **Score** = text relevance + entity match bonus + graph proximity + recency + importance. Deduplicate and keep the best blocks, not whole notes.
6. Format as compact bullets with the note title, e.g. `[Rahul] Roommate since 2024…`.

Design an `Embedder` interface so semantic search can be added later (e.g., a free embedding API) without changing callers. v1 has no embeddings.

## 5. Writing (remember)

**Explicit:** when I say "remember…", the agent calls `remember` immediately.

**Automatic curator:** after each conversation ends (idle 2 min or the orb returns to idle), `memory/curator.py` makes **one** LLM call over the conversation and returns JSON:

```json
[{"action": "add" | "update" | "delete",
  "note": "Rahul", "note_type": "person",
  "fact": "Roommate since 2024", "links": ["portfolio-site"],
  "replaces_block": "^f1", "confidence": 0.9}]
```

Rules for the curator prompt:
- Only durable facts I stated: people, relationships, projects, preferences, plans, dates. No chit-chat, no assistant suggestions, no guesses about me.
- Prefer updating an existing fact over adding a duplicate (give it the candidate notes and their blocks).
- Resolve names to existing notes via aliases before creating new ones.
- `confidence < 0.7` → write to `inbox/` instead of the main notes.
- Never store: passwords, API keys, card/bank/ID numbers, OTPs. Run a regex secret filter **before** writing, independent of the LLM.

Also append a 2–3 line summary of each conversation to `daily/YYYY-MM-DD.md`, linking the notes it touched.

**Writes must be safe:** write to a temp file then atomically replace; never lose my manual edits (re-read the file right before writing; if it changed, merge by block ID).

## 6. Agent tools

Register in `tools/memory.py` (all green tier, except `forget` which is yellow):

| Tool | Does |
|---|---|
| `remember(fact, about?, tags?)` | add/update a fact in the right note |
| `recall(query)` | run retrieval, return matching facts |
| `read_note(title)` | full note content |
| `link_notes(a, b, reason?)` | add a link between two notes |
| `list_notes(type?, tag?)` | browse |
| `forget(query)` | find matching facts, confirm, then delete them **completely** (no "used to…" leftovers) |
| `memory_stats()` | counts, recent changes |

## 7. Consolidation (nightly)

A scheduled job (APScheduler, ~3 AM or on next start if missed):
- Merge near-duplicate facts within a note.
- Move high-confidence `inbox/` items into their notes; leave unclear ones.
- Compress daily notes older than 30 days into a weekly summary note, keeping links.
- Lower `importance` slowly for notes never used; never auto-delete facts.
- Log everything it changed to `logs/memory_consolidation.jsonl` so I can review.

## 8. Memory viewer (the Obsidian-like UI)

`ui/memory.html` opened from the tray ("Open memory") in a normal PySide6 window. Communicate with Python through `QWebChannel` (no network port).

- **Graph view:** force-directed graph of notes; node size = number of links, color = note type, ghost links shown faded. Click a node to open the note; hover to highlight neighbors. Search box filters/highlights nodes. Use a graph library vendored into `ui/vendor/` so it works offline.
- **Note panel:** rendered markdown with clickable `[[links]]`, a **Backlinks** section, tags, and an Edit mode (plain textarea, saves to the file).
- **Sidebar:** note list grouped by type, tag list, "Recent", and an "Inbox" view to approve or discard uncertain facts.
- Match the orb's look: dark background, warm rust and soft lavender accents, grainy texture optional. Light and dark mode.

## 9. Modules

```
memory/
├── vault.py        # read/write notes, frontmatter, block IDs, atomic writes
├── parser.py       # wikilinks, tags, block IDs, provenance comments
├── index.py        # SQLite schema, incremental sync, watcher
├── retrieve.py     # hybrid search + graph expansion → context pack
├── curator.py      # post-conversation fact extraction
├── consolidate.py  # nightly cleanup
├── secrets.py      # regex filter for things that must never be stored
└── embed.py        # Embedder interface (no-op in v1)
```

Dependencies to add: `python-frontmatter`, `watchdog`, `python-ulid`, `markdown` (for rendering in the viewer). SQLite and FTS5 come with Python.

## 10. Tests (`tests/test_memory_*.py`, temp vault)

- Parser: wikilinks with aliases (`[[rahul|Rahul]]`), tags, block IDs, provenance.
- Index: rebuild from files equals incremental sync; editing a file by hand updates the index; renaming a note updates links.
- Retrieval: "what's my roommate working on?" returns the Rahul fact **and** the linked project.
- Curator: duplicates update instead of adding; low confidence goes to inbox; secrets are never written (test with a fake API key and card number).
- Forget: deleted facts are gone from the file and the index.
- Atomic write: a simulated crash mid-write leaves the original file intact.

## 11. Done when

- ✅ "Remember Rahul is my roommate and he's helping with my portfolio site" → creates/updates `people/rahul.md` linked to `projects/portfolio-site.md`.
- ✅ After a restart, "who's helping me with my portfolio site?" → "Rahul."
- ✅ Editing a note by hand is picked up within seconds.
- ✅ The memory viewer shows the graph, backlinks work, and I can edit and approve inbox items.
- ✅ "Forget everything about Rahul" asks first, then removes it completely.
- ✅ Retrieval adds under 100 ms per message on a vault of 1,000 notes.
- ✅ All tests pass.
