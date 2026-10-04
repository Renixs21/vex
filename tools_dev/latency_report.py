"""Where does the time go between "you stop talking" and "Vex's first word"? Reads logs/latency.jsonl (written by every turn).

    python tools_dev/latency_report.py                 # the last 30 turns, medians per stage, cut-off statistics
    python tools_dev/latency_report.py --last 10       # just your last 10 turns
    python tools_dev/latency_report.py --since "2026-10-04 17:30"
    python tools_dev/latency_report.py --json          # the summary as JSON

Stages (seconds): endpoint_wait = the silence Vex waits for before it decides you finished; stt = upload + Whisper; brain = language model
calls and tools; tts_first_audio = reply ready -> first sound; total = you stopped talking -> first word.

Cut-offs: "resumed" = you carried on talking right after the endpoint and the safeguard caught it (without the safeguard those turns would have
been answered after half a sentence). "possible residual cut-offs" = a turn whose sentence did not end cleanly (no . ? !) followed within 15 s
by another turn: the safeguard's window is short, so a longer pause can still split a sentence. Only timings, counts and flags are logged:
never what you said.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def stt_extra(rows: list[dict]) -> dict:
    """STT time next to the length of the audio, and how often the backup provider or a retry was needed."""
    pairs = [(r["audio_seconds"], r["stages"]["stt"]) for r in rows if "audio_seconds" in r and "stt" in r.get("stages", {})]
    out: dict = {"fallbacks": sum(1 for r in rows if r.get("stt_fallback")), "retries": sum(1 for r in rows if (r.get("stt_attempts") or 1) > 1)}
    if pairs:
        out["median_audio_s"] = round(statistics.median(a for a, _ in pairs), 2)
        out["median_stt_s"] = round(statistics.median(s for _, s in pairs), 2)
        out["median_stt_per_audio_s"] = round(statistics.median(s / a for a, s in pairs if a > 0), 2)
    return out


STAGES = ("trigger_to_record", "endpoint_wait", "stt", "brain", "confirm_wait", "tts_first_audio", "total")


def load(path: Path, since: datetime | None = None) -> list[dict]:
    rows: list[dict] = []
    files = sorted(path.parent.glob(path.stem + "*" + path.suffix))               # latency.jsonl plus any rotated siblings
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except (OSError, ValueError):
            continue
    rows.sort(key=lambda r: r.get("ts", ""))
    if since is not None:
        rows = [r for r in rows if datetime.fromisoformat(r["ts"]).replace(tzinfo=None) >= since]
    return rows


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def stage_stats(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for s in STAGES:
        vals = [r["stages"][s] for r in rows if s in r.get("stages", {})]
        if vals:
            out[s] = {"n": len(vals), "median": round(statistics.median(vals), 2), "p90": round(percentile(vals, 0.9), 2),
                      "max": round(max(vals), 2)}
    return out


def cutoffs(rows: list[dict]) -> dict:
    voice = [r for r in rows if "endpoint_wait" in r.get("stages", {})]
    resumed = [r for r in voice if r.get("resumed")]
    residual = 0
    for prev, nxt in zip(rows, rows[1:]):
        if prev.get("ends_clean") is False and "endpoint_wait" in prev.get("stages", {}):
            gap = (datetime.fromisoformat(nxt["ts"]) - datetime.fromisoformat(prev["ts"])).total_seconds()
            if 0 <= gap <= 15:
                residual += 1
    return {"voice_turns": len(voice), "resumed": len(resumed),
            "resumed_rate": round(len(resumed) / len(voice), 3) if voice else 0.0,
            "median_resume_gap_s": round(statistics.median([r["resume_gap"] for r in resumed if r.get("resume_gap") is not None]), 2)
            if any(r.get("resume_gap") is not None for r in resumed) else None,
            "possible_residual_cutoffs": residual}


def by_endpoint(rows: list[dict]) -> dict[str, dict]:
    groups: dict[int, list[dict]] = {}
    for r in rows:
        if "endpoint_wait" in r.get("stages", {}) and "total" in r["stages"]:
            groups.setdefault(int(r.get("endpoint_ms") or 0), []).append(r)
    out = {}
    for ms, rs in sorted(groups.items()):
        totals = [r["stages"]["total"] for r in rs]
        out[str(ms)] = {"turns": len(rs), "median_total": round(statistics.median(totals), 2),
                        "median_endpoint_wait": round(statistics.median(r["stages"]["endpoint_wait"] for r in rs), 2),
                        "resumed": sum(1 for r in rs if r.get("resumed")),
                        "estimated_median_total_at_1000ms": round(statistics.median(t + max(0, 1000 - ms) / 1000 for t in totals), 2)}
    return out


def slowest(rows: list[dict]) -> list[tuple[str, float]]:
    stats = stage_stats(rows)
    parts = {k: v["median"] for k, v in stats.items() if k in ("endpoint_wait", "stt", "brain", "tts_first_audio")}
    return sorted(parts.items(), key=lambda kv: -kv[1])


def summary(rows: list[dict]) -> dict:
    spoken = [r for r in rows if r.get("outcome") == "spoken"]
    windows = [r for r in rows if r.get("source") == "followup"]
    used = [r for r in windows if r.get("outcome") not in ("followup_expired", "followup_end")]
    return {"turns": len(rows), "spoken": len(spoken), "stages": stage_stats(spoken), "cutoffs": cutoffs(rows),
            "followup": {"windows": len(windows), "used": len(used), "expired": sum(1 for r in windows if r.get("outcome") == "followup_expired"),
                         "ended_by_phrase": sum(1 for r in windows if r.get("outcome") == "followup_end")},
            "by_endpoint_ms": by_endpoint(spoken), "slowest": slowest(spoken), "stt_detail": stt_extra(spoken),
            "llm_calls_median": statistics.median([r.get("llm_calls", 0) for r in spoken]) if spoken else None,
            "providers": sorted({c["provider"] for r in spoken for c in r.get("llm", [])})}


def fmt(v) -> str:
    return "  -  " if v is None else f"{v:5.2f}"


def print_report(rows: list[dict]) -> None:
    if not rows:
        print("No turns logged yet. Use Vex by voice (hotkey or 'Hey Vex'), then run this again.")
        return
    print(f"{'time':8} {'via':8} {'endpt':>5} {'audio':>5} {'stt':>5} {'brain':>5} {'tts':>5} {'TOTAL':>5}  notes")
    for r in rows:
        s = r.get("stages", {})
        notes = []
        if r.get("resumed"):
            notes.append(f"RESUMED after a {r.get('resume_gap', 0):.2f}s pause")
        if r.get("llm_calls", 0) > 1:
            notes.append(f"{r['llm_calls']} model calls")
        if s.get("confirm_wait"):
            notes.append(f"you took {s['confirm_wait']:.1f}s to answer a confirmation (not counted)")
        if r.get("llm_answered_by") and r.get("llm_why", "first choice") != "first choice":
            notes.append(f"answered by {r['llm_answered_by']} ({r['llm_why']})")
        if r.get("filler"):
            notes.append("said 'One sec'")
        if r.get("stt_fallback"):
            notes.append("backup STT used")
        if (r.get("stt_attempts") or 1) > 1:
            notes.append("STT retried")
        if r.get("outcome") != "spoken":
            notes.append(r.get("outcome", "?"))
        print(f"{r['ts'][11:19]} {r.get('source', '?')[:8]:8} {fmt(s.get('endpoint_wait'))} {fmt(r.get('audio_seconds'))} {fmt(s.get('stt'))} {fmt(s.get('brain'))} "
              f"{fmt(s.get('tts_first_audio'))} {fmt(s.get('total'))}  {'; '.join(notes)}")
    sm = summary(rows)
    print(f"\n{sm['spoken']} spoken turns of {sm['turns']}.   median / 90th percentile / worst, in seconds:")
    for k, v in sm["stages"].items():
        print(f"  {k:18} {v['median']:5.2f} / {v['p90']:5.2f} / {v['max']:5.2f}   ({v['n']} turns)")
    d = sm["stt_detail"]
    if "median_audio_s" in d:
        print(f"  speech recognition: median {d['median_stt_s']} s for {d['median_audio_s']} s of audio ({d['median_stt_per_audio_s']} s per audio second); "
              f"retried {d['retries']}x, backup used {d['fallbacks']}x")
    c = sm["cutoffs"]
    print(f"\nCut-offs: {c['resumed']} of {c['voice_turns']} voice turns resumed after a pause ({c['resumed_rate']:.0%}), "
          f"{c['possible_residual_cutoffs']} possible residual cut-off(s)."
          + (f" Median pause before you carried on: {c['median_resume_gap_s']} s." if c["median_resume_gap_s"] is not None else ""))
    for ms, g in sm["by_endpoint_ms"].items():
        print(f"  silence setting {ms} ms: {g['turns']} turns, median total {g['median_total']} s, median endpoint wait {g['median_endpoint_wait']} s, "
              f"resumed {g['resumed']}; the same turns would have taken about {g['estimated_median_total_at_1000ms']} s at 1000 ms")
    f = sm["followup"]
    if f["windows"]:
        print(f"\nFollow-up windows: {f['windows']} opened, {f['used']} used for another command, {f['expired']} closed in silence, "
              f"{f['ended_by_phrase']} ended by 'that's all'/'thanks'.")
    if sm["slowest"]:
        print("\nSlowest stages (median): " + ", ".join(f"{k} {v:.2f}s" for k, v in sm["slowest"][:3]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--last", type=int, default=30, help="how many of the latest turns (default 30)")
    ap.add_argument("--since", help='only turns after this local time, e.g. "2026-10-04 17:30"')
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("--file", help="a different latency.jsonl")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    import config

    path = Path(args.file) if args.file else config.ROOT / config.settings["paths"]["logs"] / "latency.jsonl"
    rows = load(path, datetime.fromisoformat(args.since) if args.since else None)
    rows = rows[-args.last:] if args.last else rows
    if args.json:
        print(json.dumps(summary(rows), indent=1, ensure_ascii=False))
    else:
        print_report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
