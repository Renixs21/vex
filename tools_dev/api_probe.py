"""A tiny client for Vex's local API (docs/API.md), for testing.

    python tools_dev/api_probe.py state
    python tools_dev/api_probe.py text "what's the time"
    python tools_dev/api_probe.py watch                       # print every event until Ctrl+C
    python tools_dev/api_probe.py call memory.search '{"query": "exam"}'
    python tools_dev/api_probe.py call tools.list
    python tools_dev/api_probe.py commands

Reads the token from data/api_token and the port from config.toml. Never prints the token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def run(cmd: str, args: dict, watch: bool, port: int, token: str, out=print) -> int:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{port}/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
            hello = await ws.receive_json(timeout=5)
            if hello.get("event") != "hello":
                out(json.dumps(hello))
                return 1
            if watch:
                out(json.dumps(hello["data"]["state"], ensure_ascii=False))
                async for msg in ws:
                    out(msg.data)
                return 0
            await ws.send_json({"id": 1, "cmd": cmd, "args": args})
            while True:
                msg = await ws.receive_json(timeout=120)
                if msg.get("id") == 1:
                    out(json.dumps(msg.get("data") if msg.get("ok") else {"error": msg.get("error")}, indent=1, ensure_ascii=False))
                    return 0 if msg.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools_dev/api_probe.py", description="Talk to Vex's local API.")
    ap.add_argument("what", help="state | text | watch | commands | call")
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    import config

    try:
        token = (config.ROOT / "data" / "api_token").read_text(encoding="utf-8").strip()
    except OSError:
        print("No data/api_token yet: start Vex once (the token is created on its first start).")
        return 1
    port = int(config.settings.get("api", {}).get("port", 47480))
    watch = a.what == "watch"
    if a.what == "text":
        cmd, args = "text", {"text": " ".join(a.rest)}
    elif a.what == "call":
        if not a.rest:
            print("call needs a command, e.g. call memory.search '{\"query\": \"exam\"}'")
            return 2
        cmd, args = a.rest[0], json.loads(a.rest[1]) if len(a.rest) > 1 else {}
    else:
        cmd, args = a.what, {}
    try:
        return asyncio.run(run(cmd, args, watch, port, token))
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"Can't reach Vex on 127.0.0.1:{port} ({e}). Is it running with [api] enabled = true?")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
