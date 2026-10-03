"""Measure VexMind retrieval time on a generated vault.

    python tools_dev/memory_bench.py            # 1000 notes
    python tools_dev/memory_bench.py --notes 2000 --queries 300
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import Memory  # noqa: E402
from memory.retrieve import retrieve  # noqa: E402

WORDS = ("garden exam music travel project deadline recipe workout budget movie guitar coffee design server bug "
         "paper lecture hiking camera laptop invoice birthday dentist flight hotel book podcast chess pizza").split()
FIRST = ["Rahul", "Priya", "Arjun", "Meera", "Kabir", "Ananya", "Vikram", "Isha", "Dev", "Sara"]


def generate(root: Path, n: int, rng: random.Random) -> list[str]:
    """Write n notes (people, projects, topics) with ~6 facts each and random cross-links. Returns note titles."""
    titles: list[str] = []
    kinds = [("people", "person"), ("projects", "project"), ("topics", "topic")]
    for i in range(n):
        folder, type_ = kinds[i % 3]
        titles.append(f"{rng.choice(FIRST)} {rng.choice(WORDS).title()} {i}" if folder == "people"
                      else f"{rng.choice(WORDS).title()} {folder[:-1].title()} {i}")
    for i, title in enumerate(titles):
        folder, type_ = kinds[i % 3]
        slug = title.lower().replace(" ", "-")
        facts = []
        for k in range(6):
            link = f" with [[{rng.choice(titles)}]]" if rng.random() < 0.5 else ""
            facts.append(f"- {' '.join(rng.choices(WORDS, k=6))}{link}  ^f{k + 1}\n"
                         f"  <!-- src: bench · learned: 2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)} -->\n")
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(
            f"---\nid: 01BENCH{i:018d}\ntype: {type_}\ntitle: {title}\naliases: [alias {i}]\ntags: [bench]\n"
            f"created: 2026-01-01T00:00:00+05:30\nupdated: 2026-01-01T00:00:00+05:30\nimportance: {rng.randint(1, 5)}\n---\n\n"
            f"# {title}\n\n{''.join(facts)}", encoding="utf-8")
    return titles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=1000)
    ap.add_argument("--queries", type=int, default=200)
    args = ap.parse_args()
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "vault"
        titles = generate(root, args.notes, rng)

        t = time.perf_counter()
        mem = Memory(root)                                  # first open: full index build
        build = time.perf_counter() - t
        t = time.perf_counter()
        mem.index.sync()
        noop = time.perf_counter() - t
        s = mem.index.stats()
        print(f"vault: {s['notes']} notes, {s['facts']} facts, {s['links']} links")
        print(f"initial index build: {build:.2f}s   no-change sync: {noop * 1000:.1f} ms")

        queries = []
        for _ in range(args.queries):
            kind = rng.choice(["entity", "text", "mixed"])
            name = rng.choice(titles)
            words = " ".join(rng.choices(WORDS, k=3))
            queries.append({"entity": f"tell me about {name}", "text": f"when is the {words}",
                            "mixed": f"what did {name} say about {words}"}[kind])
        for label, touch in (("with usage tracking", True), ("read-only", False)):
            times = []
            for q in queries:
                t = time.perf_counter()
                retrieve(mem.index, q, touch=touch)
                times.append((time.perf_counter() - t) * 1000)
            times.sort()
            print(f"retrieval {label:20s} mean {statistics.mean(times):6.1f} ms   median {times[len(times) // 2]:6.1f} ms   "
                  f"p95 {times[int(len(times) * .95)]:6.1f} ms   max {times[-1]:6.1f} ms")
        mem.close()


if __name__ == "__main__":
    main()
