"""Vex entry point: `python main.py` (tray + visual + hotkey) or `python main.py --text` (terminal chat)."""
from __future__ import annotations

import argparse
import os
import sys

import config


def run_text() -> int:
    from brain.agent import Agent
    from tools import reminders

    try:
        agent = Agent()
        reminders.start()
    except RuntimeError as e:
        print(e)
        return 1
    print("Vex (text mode). Type 'exit' to quit." + ("  [DRY RUN: nothing will be changed]" if config.dry_run() else ""))
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in ("exit", "quit"):
            return 0
        if not line:
            continue
        try:
            print("vex>", agent.run(line))
        except Exception as e:  # noqa: BLE001
            print("vex> Something went wrong:", e)


def main() -> int:
    ap = argparse.ArgumentParser(prog="vex")
    ap.add_argument("--text", action="store_true", help="terminal chat mode")
    ap.add_argument("--dry-run", action="store_true", help="log what tools would do without doing it")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # video titles/replies may contain emoji; cp1252 consoles choke on them
        stream.reconfigure(encoding="utf-8", errors="replace")
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    config.setup_logging()
    if args.text:
        return run_text()
    from ui.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
