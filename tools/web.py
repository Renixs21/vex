"""Web search via DuckDuckGo. Results are untrusted data: short snippets only, control characters stripped."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from tools.registry import ToolResult, tool

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(s: str, limit: int) -> str:
    return " ".join(_CTRL.sub(" ", s or "").split())[:limit]


def _search(query: str, n: int) -> list[dict]:
    """Raw DuckDuckGo text search (patched in tests)."""
    from ddgs import DDGS

    return list(DDGS().text(query, max_results=n))


@tool("web_search", "Search the web; returns short snippets.",
      {"query": {"type": "string", "description": "Search query"},
       "max_results": {"type": "integer", "description": "1-8", "default": 5}},
      describe=lambda a: f"Web search: {a['query']}")
def web_search(query: str, max_results: int = 5) -> ToolResult:
    n = max(1, min(int(max_results), 8))
    try:
        rows = _search(query, n)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"Search failed: {e}")
    results = [{"title": _clean(r.get("title", ""), 120), "snippet": _clean(r.get("body", ""), 240),
                "site": urlparse(r.get("href", "")).netloc, "url": r.get("href", "")} for r in rows]
    if not results:
        return ToolResult(True, f"No results for '{query}'.", [])
    lines = [f"{i}. {r['title']} ({r['site']}): {r['snippet']}" for i, r in enumerate(results, 1)]
    return ToolResult(True, "\n".join(lines), results)
