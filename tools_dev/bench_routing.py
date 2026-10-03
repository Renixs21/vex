"""Measure the model ladder with and without routing simple commands to the small model.

    python tools_dev/bench_routing.py --label routed            # route_simple = true  (small model first for simple commands)
    python tools_dev/bench_routing.py --label big-first --no-route

Uses the real ladder from config.toml (groq -> groq-small -> gemini), the same five safe commands as bench_turns.py, and reports
per-turn time, which model answered each request, tokens, and 429s. Restores volume/brightness and trashes the desktop file.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools_dev"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--no-route", action="store_true")
    ap.add_argument("--wait", type=float, default=0, help="seconds to wait first, so a previous run's rate-limit window clears")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import config
    from bench_turns import COMMANDS, Count429
    from brain.agent import Agent
    from brain.llm import build_client
    from helpers import YES, ScriptedConfirmer
    from memory import Memory
    from pycaw.pycaw import AudioUtilities
    import screen_brightness_control as sbc
    from safety.audit import AuditLog
    from safety.executor import Executor

    if args.wait:
        print(f"waiting {args.wait:.0f}s for rate limits to clear...", flush=True)
        time.sleep(args.wait)
    config.settings["llm"]["route_simple"] = not args.no_route
    fb = build_client()
    print("ladder:", [(c.name, c.model) for c in fb.clients], "| route_simple =", config.settings["llm"]["route_simple"])
    counter = Count429()
    logging.getLogger("httpx").addHandler(counter)
    logging.getLogger("httpx").setLevel(logging.INFO)

    ep = AudioUtilities.GetSpeakers().EndpointVolume
    orig = (ep.GetMasterVolumeLevelScalar(), ep.GetMute(), sbc.get_brightness()[0])
    tmp = Path(tempfile.mkdtemp())
    mem = Memory(tmp / "vault")
    ex = Executor(ScriptedConfirmer(*[YES] * 10), AuditLog(tmp / "a.jsonl"), None)
    agent = Agent(llm=fb, executor=ex, memory=mem)
    turns = []
    try:
        for category, text in COMMANDS:
            n0, c0, t = len(fb.stats), counter.n, time.monotonic()
            ex.confirmer.answers = [YES] * 5
            try:
                reply, err = agent.run(text), None
            except Exception as e:  # noqa: BLE001
                reply, err = "", f"{type(e).__name__}: {str(e)[:80]}"
            dt = time.monotonic() - t
            used = fb.stats[n0:]
            turns.append({"command": text, "seconds": round(dt, 1), "providers": [u["provider"] for u in used],
                          "prompt_tokens": sum(u["prompt"] or 0 for u in used), "completion_tokens": sum(u["completion"] or 0 for u in used),
                          "http_429": counter.n - c0, "reply": reply[:90], "error": err})
            print(f"{dt:6.1f}s  {','.join(u['provider'] for u in used) or '-':28s} 429s={counter.n - c0}  {text!r} -> {reply[:60]!r} {err or ''}")
    finally:
        ep.SetMasterVolumeLevelScalar(orig[0], None)
        ep.SetMute(orig[1], None)
        try:
            sbc.set_brightness(orig[2])
        except Exception:  # noqa: BLE001
            pass
        from send2trash import send2trash
        from safety.paths import known_folder

        note = known_folder("desktop") / "bench_note.txt"
        if note.exists():
            send2trash(str(note))
        mem.close()
    by_provider: dict = {}
    for s in fb.stats:
        d = by_provider.setdefault(s["provider"], {"requests": 0, "seconds": 0.0, "prompt": 0})
        d["requests"] += 1
        d["seconds"] += s["seconds"]
        d["prompt"] += s["prompt"] or 0
    summary = {"label": args.label, "route_simple": not args.no_route,
               "avg_turn_seconds": round(sum(t["seconds"] for t in turns) / len(turns), 1), "http_429": counter.n,
               "requests": len(fb.stats),
               "per_provider": {k: {"requests": v["requests"], "avg_request_seconds": round(v["seconds"] / v["requests"], 2),
                                    "avg_prompt_tokens": round(v["prompt"] / v["requests"])} for k, v in by_provider.items()},
               "turns": turns}
    (ROOT / "logs" / f"bench_routing_{args.label}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "turns"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
