import logging
from types import SimpleNamespace

import pytest

from brain import llm
from brain.llm import (FallbackClient, GeminiClient, GroqClient, LLMResponse, RateLimiter, parse_duration, retry_after,
                       with_retries)


class ApiError(Exception):
    def __init__(self, code, headers=None, message=None, details=None):
        super().__init__(message or f"HTTP {code}")
        self.code = code
        if headers is not None:
            self.response = SimpleNamespace(headers=headers)
        if details is not None:
            self.details = details


class Client:
    def __init__(self, name, *outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate(self, system, messages, tools):
        self.calls += 1
        out = self.outcomes.pop(0) if self.outcomes else f"{self.name}-ok"
        if isinstance(out, Exception):
            raise out
        if isinstance(out, LLMResponse):
            return out
        return LLMResponse(out, [], raw=None, usage={"prompt": 100, "completion": 5})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


USER = [{"role": "user", "text": "hi"}]


def ask(fb, messages=USER):
    return fb.generate("sys", messages, []).text


# ---- failover and cooldown -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [503, 429, 500])
def test_switches_to_groq_and_stays_for_five_minutes(code, caplog):
    clock = Clock()
    gemini = Client("gemini", ApiError(code), "gemini-back")
    groq = Client("groq")
    fb = FallbackClient([gemini, groq], cooldown=300, clock=clock)
    with caplog.at_level(logging.INFO, logger="vex.llm"):
        assert ask(fb) == "groq-ok" and gemini.calls == 1
        assert "LLM switch: gemini -> groq" in caplog.text and f"HTTP {code}" in caplog.text
        for _ in range(5):                                      # no retry cost on later turns
            clock.t += 30
            assert ask(fb) == "groq-ok"
        assert gemini.calls == 1 and groq.calls == 6
        clock.t = 1000 + 301                                    # cooldown over: Gemini gets another chance
        assert ask(fb) == "gemini-back"
        assert "cooldown over, trying gemini again" in caplog.text
    assert gemini.calls == 2


def test_cooldown_lasts_as_long_as_the_provider_said_to_wait_when_that_is_longer():
    clock = Clock()
    daily = ApiError(429, message="You exceeded your current quota. Please retry in 7h43m54.01s.")
    gemini = Client("gemini", daily, "gemini-back")
    fb = FallbackClient([gemini, Client("groq")], cooldown=300, clock=clock)
    ask(fb)
    clock.t += 3600                                             # an hour later: still benched, no wasted request
    assert ask(fb) == "groq-ok" and gemini.calls == 1
    clock.t += 7 * 3600
    assert ask(fb) == "gemini-back"


def test_failing_again_after_cooldown_switches_again(caplog):
    clock = Clock()
    gemini = Client("gemini", ApiError(503), ApiError(503))
    fb = FallbackClient([gemini, Client("groq")], cooldown=300, clock=clock)
    with caplog.at_level(logging.INFO, logger="vex.llm"):
        ask(fb)
        clock.t += 301
        assert ask(fb) == "groq-ok"
        clock.t += 10
        ask(fb)
    assert gemini.calls == 2
    assert caplog.text.count("LLM switch: gemini -> groq") == 2


def test_request_specific_errors_do_not_trigger_the_cooldown():
    gemini = Client("gemini", ApiError(400))
    fb = FallbackClient([gemini, Client("groq")], cooldown=300, clock=Clock())
    assert ask(fb) == "groq-ok"                                 # this request goes to Groq...
    assert ask(fb) == "gemini-ok"                               # ...but Gemini isn't benched


def test_cooling_provider_is_still_a_last_resort():
    clock = Clock()
    gemini = Client("gemini", ApiError(503))
    groq = Client("groq", ApiError(503), ApiError(503))
    fb = FallbackClient([gemini, groq], cooldown=300, clock=clock)
    with pytest.raises(RuntimeError):
        ask(fb)                                                 # both down
    assert ask(fb) == "gemini-ok"                               # benched Gemini is tried when Groq fails too


def test_connection_errors_count_as_provider_down():
    class ConnectError(Exception):
        pass

    gemini = Client("gemini", ConnectError("dns"))
    fb = FallbackClient([gemini, Client("groq")], cooldown=300, clock=Clock())
    ask(fb)
    ask(fb)
    assert gemini.calls == 1


def test_single_provider_has_nowhere_to_switch():
    only = Client("gemini", ApiError(503))
    fb = FallbackClient([only], clock=Clock())
    with pytest.raises(RuntimeError, match="All LLM providers failed"):
        ask(fb)
    assert ask(fb) == "gemini-ok"                               # and isn't benched


def test_no_providers_configured():
    with pytest.raises(RuntimeError, match="No LLM configured"):
        FallbackClient([])


# ---- empty replies are failures, never "Done" ----------------------------------------------------------------------------
def test_an_empty_first_reply_fails_over_without_benching_the_provider(caplog):
    gemini = Client("gemini", LLMResponse("", [], usage=None))
    groq = Client("groq")
    fb = FallbackClient([gemini, groq], cooldown=300, clock=Clock())
    with caplog.at_level(logging.WARNING, logger="vex.llm"):
        assert ask(fb) == "groq-ok"
    assert "returned an empty reply" in caplog.text and "LLM switch" not in caplog.text
    assert ask(fb) == "gemini-ok"                               # not benched: it answered the next request fine


def test_empty_reply_from_every_provider_is_an_error():
    fb = FallbackClient([Client("a", LLMResponse("", [])), Client("b", LLMResponse("  ", []))], clock=Clock())
    with pytest.raises(RuntimeError, match="empty reply"):
        ask(fb)


def test_an_empty_reply_after_tool_results_is_allowed():
    fb = FallbackClient([Client("a", LLMResponse("", []))], clock=Clock())
    after_tool = [{"role": "user", "text": "x"}, {"role": "assistant", "text": "", "tool_calls": []},
                  {"role": "tool", "id": "1", "name": "t", "result": {"ok": True}}]
    assert ask(fb, after_tool) == ""


def test_token_stats_are_recorded_per_request():
    fb = FallbackClient([Client("groq")], clock=Clock())
    ask(fb)
    assert fb.stats[0]["provider"] == "groq" and fb.stats[0]["prompt"] == 100 and fb.stats[0]["completion"] == 5


# ---- retries ----------------------------------------------------------------------------------------------------------------
def test_two_quick_attempts_then_give_up():
    calls, slept = [], []

    def flaky():
        calls.append(1)
        raise ApiError(503)

    with pytest.raises(ApiError):
        with_retries(flaky, 2, sleep=slept.append)
    assert len(calls) == 2 and slept == [1.0]


def test_second_attempt_can_succeed():
    seq = [ApiError(429), "fine"]

    def fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    assert with_retries(fn, 2, sleep=lambda s: None) == "fine"


def test_non_retryable_errors_are_not_retried():
    calls = []

    def bad():
        calls.append(1)
        raise ApiError(400)

    with pytest.raises(ApiError):
        with_retries(bad, 5, sleep=lambda s: None)
    assert len(calls) == 1


def test_a_long_retry_after_fails_over_immediately_without_sleeping():
    calls, slept = [], []

    def limited():
        calls.append(1)
        raise ApiError(429, headers={"retry-after": "15"})

    with pytest.raises(ApiError):
        with_retries(limited, 4, sleep=slept.append)
    assert len(calls) == 1 and slept == []


def test_a_short_retry_after_is_honoured_then_retried():
    seq = [ApiError(429, headers={"retry-after": "2"}), "ok"]
    slept = []

    def fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    assert with_retries(fn, 2, sleep=slept.append) == "ok" and slept == [2.0]


def test_a_lone_provider_is_willing_to_wait_longer():
    seq = [ApiError(429, headers={"retry-after": "12"}), "ok"]
    slept = []

    def fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    assert with_retries(fn, 4, fail_over_after=20.0, sleep=slept.append) == "ok" and slept == [12.0]


@pytest.mark.parametrize("text,seconds", [("7h43m54.01s", 7 * 3600 + 43 * 60 + 54.01), ("11s", 11), ("27834s", 27834),
                                          ("1m30s", 90), ("250ms", 0.25), ("12", 12), ("soon", None)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds) if seconds is not None else parse_duration(text) is None


def test_retry_after_is_read_from_headers_and_error_bodies():
    assert retry_after(ApiError(429, headers={"retry-after": "7"})) == 7
    assert retry_after(ApiError(429, headers={"retry-after-ms": "1500"})) == 1.5
    assert retry_after(ApiError(429, headers={"x-ratelimit-reset-tokens": "6s"})) == 6
    gemini_body = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "27834s"}]}}
    assert retry_after(ApiError(429, details=gemini_body)) == 27834
    assert retry_after(ApiError(429, message="quota exceeded. Please retry in 11.69728772s.")) == pytest.approx(11.697, abs=0.01)
    assert retry_after(ApiError(503)) is None


# ---- rolling-window rate limiter ----------------------------------------------------------------------------------------
def test_rate_limiter_allows_bursts_and_waits_only_when_the_window_is_full():
    now = [0.0]
    slept = []
    rl = RateLimiter(5, clock=lambda: now[0], sleep=lambda s: (slept.append(round(s, 2)), now.__setitem__(0, now[0] + s)))
    for _ in range(5):                                          # five requests in a burst: no waiting at all
        rl.wait()
        now[0] += 0.1
    assert slept == []
    rl.wait()                                                   # the sixth must wait for the first to leave the window
    assert slept == [59.5]
    now[0] += 61
    rl.wait()
    assert slept == [59.5]                                      # window has emptied again


def test_rate_limiter_is_not_a_fixed_gap():
    now = [0.0]
    rl = RateLimiter(25, clock=lambda: now[0], sleep=lambda s: None)
    for _ in range(10):
        rl.wait()                                               # ten back-to-back requests: no delay (old limiter waited 2.4 s each)
    assert len(rl._stamps) == 10 and max(rl._stamps) == 0.0


# ---- SDK settings -----------------------------------------------------------------------------------------------------------
def test_sdk_clients_have_their_own_retries_switched_off():
    g = GroqClient("openai/gpt-oss-120b", "k", 25)
    assert g.client.max_retries == 0
    gem = GeminiClient("gemini-3.5-flash-lite", "k", 5)
    assert gem.client._api_client._http_options.retry_options.attempts == 1


def test_groq_reasoning_settings_apply_only_to_gpt_oss():
    assert GroqClient("openai/gpt-oss-120b", "k", 25, reasoning_effort="low").extra_params == {
        "reasoning_effort": "low", "include_reasoning": False}
    assert GroqClient("qwen/qwen3.8-27b", "k", 25, reasoning_effort="low").extra_params == {}
    assert GroqClient("openai/gpt-oss-120b", "k", 25, reasoning_effort="").extra_params == {}


def test_groq_request_carries_the_reasoning_settings(monkeypatch):
    g = GroqClient("openai/gpt-oss-120b", "k", 1000)
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        msg = SimpleNamespace(content="Done.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))

    monkeypatch.setattr(g.client.chat.completions, "create", fake_create)
    r = g.generate("sys", USER, [])
    assert seen["reasoning_effort"] == "low" and seen["include_reasoning"] is False and r.usage == {"prompt": 10, "completion": 2}


def test_build_client_order_attempts_and_models_come_from_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY", "k2")
    fb = llm.build_client()
    assert [c.name for c in fb.clients] == ["groq", "groq-small", "gemini"]       # config: big Groq, small Groq, then Gemini
    assert all(c.attempts == 2 and c.fail_over_after == 3.0 for c in fb.clients) and fb.cooldown == 300
    assert fb.clients[0].model == llm.settings["llm"]["groq_model"] != ""
    assert fb.clients[0].reasoning_effort == llm.settings["llm"]["groq_reasoning_effort"]
    monkeypatch.setitem(llm.settings["llm"], "order", ["gemini", "groq"])
    assert [c.name for c in llm.build_client().clients] == ["gemini", "groq"]
    monkeypatch.setitem(llm.settings["llm"], "order", ["groq", "groq-small", "gemini"])
    monkeypatch.delenv("GROQ_API_KEY")
    solo = llm.build_client().clients[0]
    assert solo.attempts == 4 and solo.fail_over_after == 20.0                     # alone, so be more patient


def test_provider_without_a_configured_model_is_skipped(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY", "k2")
    monkeypatch.setitem(llm.settings["llm"], "groq_model", "")
    with caplog.at_level(logging.WARNING):
        fb = llm.build_client()
    assert [c.name for c in fb.clients] == ["groq-small", "gemini"] and "groq_model is empty" in caplog.text


# ---- model ladder and routing -----------------------------------------------------------------------------------------------
def test_default_ladder_is_big_groq_then_small_groq_then_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY", "k2")
    fb = llm.build_client()
    assert [c.name for c in fb.clients] == ["groq", "groq-small", "gemini"]
    assert [c.model for c in fb.clients[:2]] == [llm.settings["llm"]["groq_model"], llm.settings["llm"]["groq_small_model"]]
    assert fb.clients[0].model != fb.clients[1].model and fb.clients[1].reasoning_effort == "low"


def test_the_two_groq_models_are_separate_providers_with_separate_cooldowns():
    clock = Clock()
    big = Client("groq", ApiError(429, message="Rate limit reached ... on tokens per day (TPD). Please try again in 7m51.7s."))
    small, gem = Client("groq-small"), Client("gemini")
    fb = FallbackClient([big, small, gem], cooldown=300, clock=clock)
    assert ask(fb) == "groq-small-ok"                          # big is out of daily tokens: the next Groq model answers
    clock.t += 100
    assert ask(fb) == "groq-small-ok" and big.calls == 1       # big stays benched, and for as long as Groq said (471 s)
    clock.t += 400
    assert ask(fb) == "groq-ok"
    assert gem.calls == 0


def test_prefer_puts_a_provider_first_for_this_request_only():
    big, small = Client("groq"), Client("groq-small")
    fb = FallbackClient([big, small], clock=Clock())
    assert ask(fb) == "groq-ok"
    fb.prefer = "groq-small"
    assert ask(fb) == "groq-small-ok" and big.calls == 1
    fb.prefer = None
    assert ask(fb) == "groq-ok"


def test_a_failing_preferred_provider_falls_back_to_the_rest_in_order():
    small = Client("groq-small", ApiError(503))
    fb = FallbackClient([Client("groq"), small, Client("gemini")], clock=Clock())
    fb.prefer = "groq-small"
    assert ask(fb) == "groq-ok"
    fb.prefer = "groq-small"
    assert ask(fb) == "groq-ok" and small.calls == 1           # and it is benched for the cooldown


def test_prefer_for_a_provider_that_is_not_configured_changes_nothing():
    fb = FallbackClient([Client("groq"), Client("gemini")], clock=Clock())
    fb.prefer = "groq-small"
    assert ask(fb) == "groq-ok"


def test_retry_after_understands_groqs_daily_limit_message():
    e = ApiError(429, message="Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): Limit 200000, "
                              "Used 199531, Requested 1389. Please try again in 6m37.44s. Need more tokens?")
    assert retry_after(e) == pytest.approx(397.44)
    assert retry_after(ApiError(429, message="Please try again in 38.448s.")) == pytest.approx(38.448)


# ---- mixed histories: a call made by Groq, continued by Gemini ----------------------------------------------------------------
def test_function_calls_from_another_provider_get_gemini_placeholder_signature():
    from brain.llm import GEMINI_FOREIGN_CALL_SIGNATURE, ToolCall

    g = GeminiClient("gemini-3.5-flash-lite", "k", 5)
    history = [{"role": "user", "text": "set the volume"},
               {"role": "assistant", "text": "", "tool_calls": [ToolCall("set_volume", {"level": 20}, id="a"),
                                                                   ToolCall("mute", {}, id="b")], "raw": None, "raw_provider": "groq"},
               {"role": "tool", "id": "a", "name": "set_volume", "result": {"ok": True}},
               {"role": "tool", "id": "b", "name": "mute", "result": {"ok": True}}]
    contents = g._contents(history)
    model_parts = contents[1].parts
    assert [p.function_call.name for p in model_parts] == ["set_volume", "mute"]
    assert all(p.thought_signature == GEMINI_FOREIGN_CALL_SIGNATURE for p in model_parts)
    assert len(contents[2].parts) == 2                                       # both results merged into one user turn


def test_gemini_calls_keep_their_own_signed_content():
    from google.genai import types

    g = GeminiClient("gemini-3.5-flash-lite", "k", 5)
    own = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="x", args={}), thought_signature=b"real")])
    contents = g._contents([{"role": "user", "text": "hi"}, {"role": "assistant", "text": "", "tool_calls": [], "raw": own,
                                                              "raw_provider": "gemini"}])
    assert contents[1] is own and contents[1].parts[0].thought_signature == b"real"
