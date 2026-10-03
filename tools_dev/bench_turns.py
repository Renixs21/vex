"""Measure average turn time and tokens per request for a fixed set of 5 safe commands, per provider.

    python tools_dev/bench_turns.py --provider groq   --label before
    python tools_dev/bench_turns.py --provider gemini --label after

Real LLM and real tools (volume, brightness, a desktop file, PowerShell Get-Date, memory in a temp vault); confirmations are
approved by a script. Volume and brightness are restored and the desktop file is moved to the Recycle Bin afterwards.
Writes logs/bench_<label>_<provider>.json. Never touches shutdown, restart, lock or UAC.
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

COMMANDS = [
    ("system", "set the volume to 20 percent"),
    ("system", "set the brightness to 40"),
    ("files", "create a file bench_note.txt on my desktop saying hi"),
    ("shell", "run powershell: Get-Date -Format yyyy-MM-dd"),
    ("memory", "remember that my roommate is Rahul"),
]


class Count429(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "429" in record.getMessage() and "HTTP Request" in record.getMessage():
            self.n += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["groq", "gemini"], required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--all-tools", action="store_true", help="send every tool every time (the old behaviour)")
    ap.add_argument("--reasoning", default=None, help="override [llm] groq_reasoning_effort ('' = send no reasoning params)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import config
    from brain.agent import Agent
    from brain.llm import FallbackClient, build_client
    from helpers import ScriptedConfirmer, YES
    from memory import Memory
    from pycaw.pycaw import AudioUtilities
    import screen_brightness_control as sbc
    from safety.audit import AuditLog
    from safety.executor import Executor

    cfg = config.settings["llm"]
    cfg["order"] = [args.provider]
    cfg["tool_groups"] = not args.all_tools
    if args.reasoning is not None:
        cfg["groq_reasoning_effort"] = args.reasoning                      # one provider alone, built exactly as the real solo configuration is
    fb_real = build_client()
    client = fb_real.clients[0]
    sdk_call_owner, sdk_call_name = ((client.client.chat.completions, "create") if args.provider == "groq"
                                     else (client.client.models, "generate_content"))

    records: list[dict] = []
    original = getattr(sdk_call_owner, sdk_call_name)

    def wrapped(*a, **kw):
        t = time.monotonic()
        rec = {"seconds": None, "prompt_tokens": None, "completion_tokens": None, "tools_sent": None, "error": None}
        if args.provider == "groq":
            rec["tools_sent"] = len(kw.get("tools") or [])
        else:
            tl = (kw.get("config").tools or []) if kw.get("config") else []
            rec["tools_sent"] = sum(len(x.function_declarations or []) for x in tl)
        try:
            r = original(*a, **kw)
            if args.provider == "groq":
                rec["prompt_tokens"], rec["completion_tokens"] = r.usage.prompt_tokens, r.usage.completion_tokens
            else:
                u = r.usage_metadata
                rec["prompt_tokens"], rec["completion_tokens"] = u.prompt_token_count, u.candidates_token_count
            return r
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            raise
        finally:
            rec["seconds"] = round(time.monotonic() - t, 2)
            records.append(rec)

    setattr(sdk_call_owner, sdk_call_name, wrapped)

    counter = Count429()
    logging.getLogger("httpx").addHandler(counter)
    logging.getLogger("httpx").setLevel(logging.INFO)

    ep = AudioUtilities.GetSpeakers().EndpointVolume
    orig_vol, orig_mute, orig_bright = ep.GetMasterVolumeLevelScalar(), ep.GetMute(), sbc.get_brightness()[0]
    tmp = Path(tempfile.mkdtemp())
    mem = Memory(tmp / "vault")
    ex = Executor(ScriptedConfirmer(*[YES] * 10), AuditLog(tmp / "audit.jsonl"), None)
    agent = Agent(llm=fb_real, executor=ex, memory=mem)

    turns = []
    try:
        for category, text in COMMANDS:
            n0, c0 = len(records), counter.n
            ex.confirmer.answers = [YES] * 5
            t = time.monotonic()
            try:
                reply = agent.run(text)
                err = None
            except Exception as e:  # noqa: BLE001
                reply, err = "", f"{type(e).__name__}: {str(e)[:100]}"
            dt = time.monotonic() - t
            reqs = records[n0:]
            turns.append({"command": text, "category": category, "seconds": round(dt, 1), "requests": len(reqs),
                          "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in reqs),
                          "completion_tokens": sum(r["completion_tokens"] or 0 for r in reqs),
                          "tools_sent": [r["tools_sent"] for r in reqs], "http_429": counter.n - c0,
                          "reply": reply[:160], "error": err})
            print(f"{dt:6.1f}s  req={len(reqs)}  prompt_tok={turns[-1]['prompt_tokens']:5d}  429s={counter.n - c0}  {text!r} -> {reply[:70]!r} {err or ''}")
    finally:
        ep.SetMasterVolumeLevelScalar(orig_vol, None)
        ep.SetMute(orig_mute, None)
        try:
            sbc.set_brightness(orig_bright)
        except Exception:  # noqa: BLE001
            pass
        from send2trash import send2trash
        from safety.paths import known_folder

        note = known_folder("desktop") / "bench_note.txt"
        if note.exists():
            send2trash(str(note))
        mem.close()

    ok = [r for r in records if not r["error"]]
    summary = {
        "label": args.label, "provider": args.provider, "model": client.model,
        "avg_turn_seconds": round(sum(t["seconds"] for t in turns) / len(turns), 1),
        "total_requests": len(records), "failed_requests": len(records) - len(ok), "http_429": counter.n,
        "avg_prompt_tokens_per_request": round(sum(r["prompt_tokens"] or 0 for r in ok) / max(1, len(ok))),
        "avg_completion_tokens_per_request": round(sum(r["completion_tokens"] or 0 for r in ok) / max(1, len(ok))),
        "avg_tools_sent": round(sum(r["tools_sent"] or 0 for r in records) / max(1, len(records)), 1),
        "turns": turns,
    }
    out = ROOT / "logs" / f"bench_{args.label}_{args.provider}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "turns"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
