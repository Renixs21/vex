"""Per-model usage counting (brain/usage.py) and the Budget API."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

from test_followup import make

from brain import usage
from brain.llm import FallbackClient, LLMResponse
from brain.usage import LlmUsage
from ui.api_core import ApiCore


class Day:
    def __init__(self):
        self.d = date(2026, 10, 5)

    def __call__(self):
        return self.d


def test_counts_requests_and_tokens_per_model_per_day(tmp_path):
    day = Day()
    u = LlmUsage(tmp_path / "u.json", today=day)
    u.record("groq", "openai/gpt-oss-120b", 1000, 50)
    u.record("groq", "openai/gpt-oss-120b", 1200, 40)
    u.record("gemini", "gemini-flash", 900, None)
    day.d -= timedelta(days=1)
    u.record("groq", "openai/gpt-oss-120b", 500, 10)
    day.d += timedelta(days=1)
    s = u.summary(7)
    groq = next(m for m in s["models"] if m["provider"] == "groq")
    assert groq["today"] == {"requests": 2, "prompt": 2200, "completion": 90, "tokens": 2290}
    assert groq["totals"][-2:] == [510, 2290] and len(s["days"]) == 7 and s["days"][-1] == "2026-10-05"
    raw = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    assert set(raw) == {"2026-10-04", "2026-10-05"} and "prompt" in raw["2026-10-05"]["gemini"]   # counts only, no text


def test_old_days_are_dropped(tmp_path):
    day = Day()
    u = LlmUsage(tmp_path / "u.json", today=day)
    day.d -= timedelta(days=60)
    u.record("groq", "m", 1, 1)
    day.d += timedelta(days=60)
    u.record("groq", "m", 1, 1)
    assert list(json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))) == ["2026-10-05"]


def test_every_successful_model_call_is_recorded_and_nothing_without_a_recorder(tmp_path, monkeypatch):
    class P:
        name, model = "groq-small", "openai/gpt-oss-20b"

        def generate(self, system, messages, tools):
            return LLMResponse("ok", [], usage={"prompt": 800, "completion": 20})

    fb = FallbackClient([P()])
    fb.generate("s", [{"role": "user", "text": "hi"}], [])                 # no recorder installed: nothing happens
    rec = LlmUsage(tmp_path / "u.json")
    usage.set_recorder(rec)
    try:
        fb.generate("s", [{"role": "user", "text": "hi"}], [])
    finally:
        usage.set_recorder(None)
    m = rec.summary(1)["models"][0]
    assert m["provider"] == "groq-small" and m["model"] == "openai/gpt-oss-20b" and m["today"]["tokens"] == 820


def test_budget_models_api(tmp_path):
    rec = LlmUsage(tmp_path / "llm_usage.json")
    rec.record("gemini", "gemini-flash", 100, 5)
    rec.record("groq", "openai/gpt-oss-120b", 150_000, 2_000)
    c, *_ = make()
    core = ApiCore(controller=c, logs=tmp_path, data=tmp_path)
    s = core.budget_models({})
    by = {m["provider"]: m for m in s["models"]}
    assert by["groq"]["daily_limit"] == 200_000 and by["gemini"]["daily_limit"] is None
    assert by["groq"]["today"]["tokens"] == 152_000 and all(m["spend_inr"] == 0.0 for m in s["models"])
    assert [m["provider"] for m in s["models"]][0] == "groq"                                        # in the ladder's order
