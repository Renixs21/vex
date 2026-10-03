"""CLI for testing VexMind without the agent:  python -m memory <command>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from memory.api import Memory


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m memory", description="VexMind memory CLI")
    ap.add_argument("--vault", type=Path, help="vault folder (default: VexVault from config.toml)")
    ap.add_argument("--json", action="store_true", help="print machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("remember", help="save a fact")
    p.add_argument("fact")
    p.add_argument("--about", help="note the fact belongs to (created if missing)")
    p.add_argument("--type", dest="note_type", help="type for a new note: person, project, topic, place, ...")
    p.add_argument("--tag", action="append", default=[], help="tag (repeatable)")
    p.add_argument("--alias", action="append", default=[], help="another name for the note, e.g. 'my roommate'")

    p = sub.add_parser("recall", help="find facts relevant to a question")
    p.add_argument("query")

    p = sub.add_parser("forget", help="forget facts or a whole note (previews and asks first; notes go to the Recycle Bin)")
    p.add_argument("query")
    p.add_argument("--yes", "-y", action="store_true", help="skip all prompts (mentions use --mentions)")
    p.add_argument("--mentions", choices=["redact", "delete"], default="redact",
                   help="facts that only mention the person: remove just the name (default) or delete the fact")

    sub.add_parser("reindex", help="rebuild the index from the markdown files")
    sub.add_parser("stats", help="counts and recent changes")

    p = sub.add_parser("read", help="print a note")
    p.add_argument("title")

    p = sub.add_parser("list", help="list notes")
    p.add_argument("--type")
    p.add_argument("--tag")

    p = sub.add_parser("link", help="link note A to note B")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--reason")

    p = sub.add_parser("rename", help="rename a note and fix links to it")
    p.add_argument("old")
    p.add_argument("new")
    return ap


def run(args: argparse.Namespace) -> int:
    mem = Memory(args.vault)
    try:
        if args.cmd == "remember":
            res = mem.remember(args.fact, about=args.about, tags=args.tag, note_type=args.note_type, source="cli",
                               aliases=args.alias)
        elif args.cmd == "recall":
            res = mem.recall(args.query)
        elif args.cmd == "forget":
            res = mem.forget(args.query)
            if res.needs_confirmation:
                print("This is what I would forget:\n" + res.message)
                if not args.yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("Cancelled. Nothing was changed.")
                    return 1
                choices = {}
                for m in res.data["mentions"]:
                    if args.yes or not m["new_text"]:
                        continue                                    # --yes keeps the default (remove the name)
                    ans = input(f"  [{m['note']}] {m['text']}\n    [r]emove just the name (default) or [d]elete the fact? ")
                    if ans.strip().lower().startswith("d"):
                        choices[(m["note_id"], m["block_id"])] = "delete"
                res = mem.forget(args.query, confirm=True, mentions=args.mentions, choices=choices)
        elif args.cmd == "reindex":
            res = mem.reindex()
        elif args.cmd == "stats":
            res = mem.memory_stats()
        elif args.cmd == "read":
            res = mem.read_note(args.title)
        elif args.cmd == "list":
            res = mem.list_notes(args.type, args.tag)
        elif args.cmd == "link":
            res = mem.link_notes(args.a, args.b, args.reason)
        else:
            res = mem.rename_note(args.old, args.new)
    finally:
        mem.close()
    if args.json:
        print(json.dumps({"ok": res.ok, "message": res.message, "data": res.data}, ensure_ascii=False, default=str))
    else:
        print(res.message)
    return 0 if res.ok else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
