"""Compare Groq models on tool calling: does the first reply to a command call the right tool with the right arguments?

    python tools_dev/eval_models.py openai/gpt-oss-120b openai/gpt-oss-20b qwen/qwen3.8-27b

Real requests, nothing is executed. Models run in parallel (each has its own rate-limit bucket) and are paced to stay under the
free tier's 8,000 tokens/minute. Writes logs/eval_models.json.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (message, expected tool or None for a plain answer, predicate on the args)
CASES = [
    ("set the volume to 20 percent", "set_volume", lambda a: int(a.get("level", -1)) == 20),
    ("mute", "mute", lambda a: a.get("muted", True) is True),
    ("unmute the sound", "mute", lambda a: a.get("muted") is False),
    ("set brightness to 40", "set_brightness", lambda a: int(a.get("level", -1)) == 40),
    ("take a screenshot", "screenshot", lambda a: True),
    ("how much battery do I have left?", "system_info", lambda a: True),
    ("open notepad", "open_app", lambda a: "notepad" in a.get("name", "").lower()),
    ("play lofi beats on youtube", "play_youtube", lambda a: "lofi" in a.get("query", "").lower()),
    ("pause the music", "media_control", lambda a: a.get("action") == "play_pause"),
    ("next track", "media_control", lambda a: a.get("action") == "next"),
    ("add buy milk to my tasks", "add_task", lambda a: "milk" in a.get("text", "").lower()),
    ("remind me in 20 minutes to stretch", "set_reminder", lambda a: "stretch" in a.get("text", "").lower() and "20" in a.get("when", "")),
    ("what's on my to-do list?", "list_tasks", lambda a: True),
    ("close notepad", "close_app", lambda a: "notepad" in a.get("name", "").lower()),
    ("search the web for python 3.13 release date", "web_search", lambda a: "3.13" in a.get("query", "")),
    ("remember that my exam is on the 20th", "remember", lambda a: "20" in a.get("fact", "")),
    ("who is my roommate?", "recall", lambda a: True),
    ("what is two plus two?", None, lambda a: True),
    ("lock my pc", "lock_pc", lambda a: True),
    ("create a file notes.txt on my desktop saying hi", "create_file", lambda a: a.get("path", "").lower().endswith("notes.txt") and "hi" in a.get("content", "").lower()),
    ("set the volume to 35", "set_volume", lambda a: int(a.get("level", -1)) == 35),
    ("play some jazz", "play_youtube", lambda a: "jazz" in a.get("query", "").lower()),
]


def run_model(model: str, results: dict, pace: float) -> None:
    import config
    import tools
    from brain.llm import GroqClient
    from brain.prompts import SYSTEM_PROMPT
    from tools import categories, registry

    tools.load_all()
    client = GroqClient(model, config.env("GROQ_API_KEY"), 1000, attempts=4, fail_over_after=30.0,
                        reasoning_effort=config.settings["llm"]["groq_reasoning_effort"])
    system = SYSTEM_PROMPT.replace("{categories}", categories.category_summary())
    rows = []
    for text, expected, check in CASES:
        offered = registry.tools_for(categories.match(text))
        t = time.monotonic()
        try:
            r = client.generate(system, [{"role": "user", "text": text}], offered)
            err = None
        except Exception as e:  # noqa: BLE001
            r, err = None, f"{type(e).__name__}: {str(e)[:80]}"
        dt = time.monotonic() - t
        if r is None:
            ok, got = False, err
        elif expected is None:
            ok, got = (not r.tool_calls and bool(r.text)), (r.text[:40] if not r.tool_calls else f"called {r.tool_calls[0].name}")
        else:
            call = r.tool_calls[0] if r.tool_calls else None
            ok = bool(call and call.name == expected and _safe(check, call.args))
            got = f"{call.name} {json.dumps(call.args)[:60]}" if call else f"no tool call: {r.text[:40]!r}"
        rows.append({"command": text, "expected": expected, "ok": ok, "got": got, "seconds": round(dt, 2),
                     "prompt_tokens": (r.usage or {}).get("prompt") if r else None})
        time.sleep(max(0.0, pace - dt))
    results[model] = rows


def _safe(check, args) -> bool:
    try:
        return bool(check(args))
    except (TypeError, ValueError):
        return False


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    models = sys.argv[1:] or ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    results: dict = {}
    threads = [threading.Thread(target=run_model, args=(m, results, 7.5)) for m in models]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for m in models:
        rows = results.get(m, [])
        ok = sum(r["ok"] for r in rows)
        times = sorted(r["seconds"] for r in rows)
        print(f"\n{m}: {ok}/{len(rows)} correct, median {times[len(times) // 2]:.2f}s, "
              f"avg prompt tokens {sum(r['prompt_tokens'] or 0 for r in rows) // max(1, len(rows))}")
        for r in rows:
            if not r["ok"]:
                print(f"   MISS {r['command']!r}: expected {r['expected']}, got {r['got']}")
    (ROOT / "logs" / "eval_models.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
