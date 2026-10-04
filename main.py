"""Vex entry point.

    python main.py             the app: tray + orb + hotkey + voice. Prints what it started and where the log is.
    python main.py --console   same, and streams every log line to this terminal (stay in the foreground, Ctrl+C quits)
    python main.py --text      terminal chat
    python main.py --supervise keep Vex running: restart it if it crashes (3 times in 10 minutes), never after Quit Vex
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
        from tools import registry

        registry.load_disabled()                 # tools switched off through the local API stay off here too
        reminders.start()
    except RuntimeError as e:
        print(e)
        return 1
    curator = None
    if config.settings["memory"]["curator"]:
        try:
            from brain.llm import build_client
            from memory.curator import Curator
            from tools import memory as memory_tools

            mcfg = config.settings["memory"]
            curator = Curator(memory_tools.get_memory(), llm_factory=lambda: build_client(order=mcfg["curator_order"]),
                              idle_seconds=float(mcfg["curator_idle_seconds"]), min_words=int(mcfg["curator_min_words"]),
                              max_attempts=int(mcfg["curator_max_attempts"]),
                              log_path=config.ROOT / config.settings["paths"]["logs"] / "curator.jsonl")
            curator.start()
        except Exception as e:  # noqa: BLE001 - memory is a bonus
            print("(memory curator is off:", e, ")")
    print("Vex (text mode). Type 'exit' to quit." + ("  [DRY RUN: nothing will be changed]" if config.dry_run() else ""))
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.lower() in ("exit", "quit"):
            if curator is not None:
                curator.stop()               # an open conversation stays queued and is processed next time
            return 0
        if not line:
            continue
        try:
            reply = agent.run(line)
            print("vex>", reply)
            if curator is not None:
                curator.add_turn(line, reply)
        except Exception as e:  # noqa: BLE001
            print("vex> Something went wrong:", e)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vex")
    ap.add_argument("--text", action="store_true", help="terminal chat mode")
    ap.add_argument("--console", action="store_true", help="stay in the foreground and print every log line to this terminal")
    ap.add_argument("--dry-run", action="store_true", help="log what tools would do without doing it")
    ap.add_argument("--supervise", action="store_true", help="run Vex under the supervisor (what Windows autostart uses)")
    ap.add_argument("--startup-delay", type=float, default=0.0, help="with --supervise: wait this many seconds before starting")
    ap.add_argument("--quiet", action="store_true", help="start quietly: tray only, no toasts for the first minute, no sounds")
    ap.add_argument("--restarted", type=int, default=0, help=argparse.SUPPRESS)       # set by the supervisor on a restart
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # video titles/replies may contain emoji; cp1252 consoles choke on them
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.supervise:
        from pathlib import Path

        from ui import supervisor

        logs = config.ROOT / config.settings["paths"]["logs"]
        supervisor.setup_logging(logs)
        return supervisor.run(config.ROOT, logs, startup_delay=args.startup_delay, quiet=args.quiet)
    config.setup_logging(console=args.console)
    if args.text:
        return run_text()

    from ui import single_instance

    ok, other = single_instance.acquire()
    if not ok:
        who = f" (process {other})" if other else ""
        print(f"Vex is already running{who}. Look for its icon in the system tray, or close that copy first "
              "(tray menu > Quit Vex). Not starting a second one.")
        return 3                                                  # the supervisor reads 3 as "already running", not a crash
    from ui.app import run

    return run(console=args.console, quiet=args.quiet, restarted=args.restarted)


if __name__ == "__main__":
    sys.exit(main())
