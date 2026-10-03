"""Vex entry point.

    python main.py             the app: tray + orb + hotkey + voice. Prints what it started and where the log is.
    python main.py --console   same, and streams every log line to this terminal (stay in the foreground, Ctrl+C quits)
    python main.py --text      terminal chat
    ... --dry-run              tools only report what they would do
"""
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vex")
    ap.add_argument("--text", action="store_true", help="terminal chat mode")
    ap.add_argument("--console", action="store_true", help="stay in the foreground and print every log line to this terminal")
    ap.add_argument("--dry-run", action="store_true", help="log what tools would do without doing it")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # video titles/replies may contain emoji; cp1252 consoles choke on them
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    config.setup_logging(console=args.console)
    if args.text:
        return run_text()

    from ui import single_instance

    ok, other = single_instance.acquire()
    if not ok:
        who = f" (process {other})" if other else ""
        print(f"Vex is already running{who}. Look for its icon in the system tray, or close that copy first "
              "(tray menu > Quit Vex). Not starting a second one.")
        return 1
    from ui.app import run

    return run(console=args.console)


if __name__ == "__main__":
    sys.exit(main())
