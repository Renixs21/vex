"""LLM clients behind one interface (Groq and Gemini, in the order set in config.toml), with rate limiting and failover.

Only Vex controls retry timing: the SDKs' own hidden retries are switched off (they added 15-20 s waits on top of ours).
  * Two quick attempts per provider on 429/5xx, then fail over to the next provider.
  * A 429 that says "retry after more than ~3 s" fails over at once (switching is cheaper than waiting).
  * A provider that fails over is skipped for `failover_cooldown` seconds, or as long as it said to wait if that is longer
    (a daily quota can mean hours). Every switch is logged.
  * An empty first reply (no text and no tool call) counts as a failed request, never as "Done".

Neutral message format (dicts):
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [ToolCall...], "raw": provider-specific or None}
  {"role": "tool", "id": str, "name": str, "result": dict}
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from config import env, settings
from tools.registry import Tool

log = logging.getLogger("vex.llm")

GEMINI_FOREIGN_CALL_SIGNATURE = b"skip_thought_signature_validator"
RETRY_AFTER_FAILOVER = 3.0          # seconds: a longer "retry after" means switch provider instead of waiting
MAX_COOLDOWN = 6 * 3600.0


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    raw: Any = None
    usage: dict | None = None        # {"prompt": int|None, "completion": int|None}


class LLMClient(Protocol):
    name: str

    def generate(self, system: str, messages: list[dict], tools: list[Tool]) -> LLMResponse: ...


class RateLimiter:
    """At most `rpm` requests in any rolling 60 s window; bursts are fine, it only waits when the window is full."""

    def __init__(self, rpm: float, window: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.limit = max(1, int(rpm))
        self.window = window
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            while self._stamps and self._stamps[0] <= now - self.window:
                self._stamps.popleft()
            delay = 0.0
            if len(self._stamps) >= self.limit:
                delay = self._stamps[len(self._stamps) - self.limit] + self.window - now
            self._stamps.append(now + max(delay, 0.0))
        if delay > 0:
            self._sleep(delay)


# ---- error classification ---------------------------------------------------------------------------------------------
def _status(e: BaseException) -> int | None:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


def _retryable(e: BaseException) -> bool:
    """Rate limits (429) and transient server errors (5xx) are worth a quick retry."""
    return _status(e) in (429, 500, 502, 503, 504)


TOOL_REMINDER = ("\nCall only the tools in the provided tool list, using exactly their names. If you need a capability that "
                 "isn't listed, call more_tools with its category.\n")


def _tool_use_failed(e: BaseException) -> bool:
    """Groq's HTTP 400 'tool_use_failed': the model called a tool outside request.tools or produced a malformed call."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    return _status(e) == 400 and isinstance(err, dict) and err.get("code") == "tool_use_failed"


def _failed_call(e: BaseException) -> str:
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    return str(err.get("failed_generation", ""))[:80] if isinstance(err, dict) else ""


def _provider_down(e: BaseException) -> bool:
    """True when the provider itself is the problem (limits, outage, bad key, network), not this one request."""
    name = type(e).__name__.lower()
    return (_retryable(e) or _status(e) in (401, 403, 404)
            or any(w in name for w in ("connect", "timeout", "unavailable", "network")))     # ConnectError, ReadTimeout...


_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float | None:
    """Seconds in '7h43m54.01s', '11s', '27834s', '1m30s', '250ms' or a bare number; None if there is none."""
    text = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)(?![a-z])", text)
    return sum(float(n) * _UNITS[u] for n, u in parts) if parts else None


def retry_after(e: BaseException) -> float | None:
    """How long the provider says to wait, from the Retry-After header, rate-limit reset headers, or the error body."""
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers is not None:
        try:
            if (ms := headers.get("retry-after-ms")) is not None:
                return float(ms) / 1000
            for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
                if (v := headers.get(name)) is not None and (d := parse_duration(str(v))) is not None:
                    return d
        except (TypeError, ValueError):
            pass
    details = getattr(e, "details", None)
    blob = json.dumps(details, default=str) if details is not None else ""
    for text in (blob, str(e)):
        m = (re.search(r"retryDelay['\"]?\s*:\s*['\"]?([0-9hms.]+)", text)
             or re.search(r"(?:[Rr]etry|[Tt]ry again) in ([0-9hms.]+)", text))               # Gemini / Groq wording
        if m and (d := parse_duration(m.group(1).rstrip("."))) is not None:
            return d
    return None


def with_retries(fn, attempts: int, fail_over_after: float = RETRY_AFTER_FAILOVER, sleep: Callable[[float], None] = time.sleep):
    """Call `fn`, retrying quickly on 429/5xx up to `attempts` times in total.

    If the provider says to wait longer than `fail_over_after` seconds the error is raised straight away, so the caller can
    fail over to another provider instead of sitting through the wait.
    """
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not _retryable(e) or i >= attempts - 1:
                raise
            ra = retry_after(e)
            if ra is not None and ra > fail_over_after:
                raise
            backoff = min(2.0 ** i, 6.0)
            sleep(min(max(ra or 0.0, backoff), max(fail_over_after, backoff)))


def _usage_log(provider: str, model: str, tools: list[Tool], usage: dict | None, seconds: float) -> None:
    u = usage or {}
    log.info("LLM request provider=%s model=%s tools=%d prompt_tokens=%s completion_tokens=%s time=%.1fs",
             provider, model, len(tools), u.get("prompt"), u.get("completion"), seconds)


class GeminiClient:
    name = "gemini"

    def __init__(self, model: str, api_key: str, rpm: float, attempts: int = 2, fail_over_after: float = RETRY_AFTER_FAILOVER):
        from google import genai
        from google.genai import types

        self.model = model
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1)))          # no hidden SDK retries
        self.limiter = RateLimiter(rpm)
        self.attempts = attempts
        self.fail_over_after = fail_over_after

    def _contents(self, messages: list[dict]):
        from google.genai import types

        out: list = []
        for m in messages:
            if m["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=m["text"])]))
            elif m["role"] == "assistant":
                if m.get("raw") is not None and m.get("raw_provider") == "gemini":
                    out.append(m["raw"])  # keeps thought signatures intact
                else:
                    parts = [types.Part(text=m["text"])] if m["text"] else []
                    # Gemini 3 rejects history function calls without a thought signature. Calls made by another provider
                    # (Groq) have none, so they get Google's documented placeholder, which skips the check.
                    parts += [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args),
                                         thought_signature=GEMINI_FOREIGN_CALL_SIGNATURE) for c in m["tool_calls"]]
                    out.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
            else:  # tool result
                part = types.Part(function_response=types.FunctionResponse(name=m["name"], response=m["result"]))
                if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[0].function_response:
                    out[-1].parts.append(part)  # merge parallel results into one turn
                else:
                    out.append(types.Content(role="user", parts=[part]))
        return out

    def generate(self, system: str, messages: list[dict], tools: list[Tool]) -> LLMResponse:
        from google.genai import types

        decls = [types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                 for t in tools]
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = self._contents(messages)

        def call():
            self.limiter.wait()
            return self.client.models.generate_content(model=self.model, contents=contents, config=cfg)

        t0 = time.monotonic()
        resp = with_retries(call, self.attempts, self.fail_over_after)
        content = resp.candidates[0].content if resp.candidates else None
        parts = (content.parts or []) if content else []
        text = "".join(p.text for p in parts if p.text and not p.thought)
        calls = [ToolCall(p.function_call.name, dict(p.function_call.args or {})) for p in parts if p.function_call]
        um = getattr(resp, "usage_metadata", None)
        usage = {"prompt": getattr(um, "prompt_token_count", None), "completion": getattr(um, "candidates_token_count", None)}
        _usage_log(self.name, self.model, tools, usage, time.monotonic() - t0)
        return LLMResponse(text.strip(), calls, raw=content, usage=usage)


class GroqClient:
    name = "groq"

    def __init__(self, model: str, api_key: str, rpm: float, attempts: int = 2, fail_over_after: float = RETRY_AFTER_FAILOVER,
                 reasoning_effort: str = "low", name: str = "groq"):
        from groq import Groq

        self.name = name                                                # "groq", "groq-small": each model has its own limits
        self.model = model
        self.client = Groq(api_key=api_key, max_retries=0)              # no hidden SDK retries
        self.limiter = RateLimiter(rpm)
        self.attempts = attempts
        self.fail_over_after = fail_over_after
        self.reasoning_effort = reasoning_effort

    @property
    def extra_params(self) -> dict:
        """gpt-oss models think before answering and can leak that into the reply; keep it short and keep it out."""
        if self.model.startswith("openai/gpt-oss") and self.reasoning_effort:
            return {"reasoning_effort": self.reasoning_effort, "include_reasoning": False}
        return {}

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                msg: dict = {"role": "assistant", "content": m["text"] or None}
                if m["tool_calls"]:
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": m["id"], "content": json.dumps(m["result"])})
        return out

    def generate(self, system: str, messages: list[dict], tools: list[Tool]) -> LLMResponse:
        kwargs: dict = dict(self.extra_params)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                   "parameters": t.parameters}} for t in tools]
        msgs = self._messages(system, messages)

        def call():
            self.limiter.wait()
            return self.client.chat.completions.create(model=self.model, messages=msgs, **kwargs)

        t0 = time.monotonic()
        try:
            resp = with_retries(call, self.attempts, self.fail_over_after)
        except Exception as e:  # noqa: BLE001
            if not _tool_use_failed(e):
                raise
            # The model called a tool that wasn't offered (e.g. 'shell.exec'); Groq rejects that with HTTP 400. Retry once
            # with a reminder instead of dropping to the next provider.
            log.warning("%s called a tool that wasn't offered (%s); retrying with a reminder", self.name, _failed_call(e))
            msgs = self._messages(system + TOOL_REMINDER, messages)
            resp = with_retries(call, self.attempts, self.fail_over_after)
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(c.function.name, args, id=c.id))
        u = getattr(resp, "usage", None)
        usage = {"prompt": getattr(u, "prompt_tokens", None), "completion": getattr(u, "completion_tokens", None)}
        _usage_log(self.name, self.model, tools, usage, time.monotonic() - t0)
        return LLMResponse((msg.content or "").strip(), calls, usage=usage)


class EmptyReply(Exception):
    """The model answered with nothing: no text and no tool call."""


class FallbackClient:
    """Try each client in order; move on when one fails.

    `resp.raw` is returned as (provider_name, raw) so the agent can replay it only to the same provider.
    """

    name = "fallback"

    def __init__(self, clients: list[LLMClient], cooldown: float = 300.0, clock=time.monotonic):
        if not clients:
            raise RuntimeError("No LLM configured. Add GROQ_API_KEY and/or GEMINI_API_KEY to .env.")
        self.clients = clients
        self.cooldown = cooldown
        self._clock = clock
        self._off_until: dict[str, float] = {}      # provider name -> time before which we don't try it first
        self.stats: list[dict] = []                 # one record per successful request: provider, tokens, tools, seconds
        self.prefer: str | None = None              # try this provider first (set per message by the agent's router)

    def _order(self) -> list[LLMClient]:
        """Providers in preference order, with any that are cooling down moved to the end (last resort)."""
        now = self._clock()
        ready = [c for c in self.clients if self._off_until.get(c.name, 0) <= now]
        cooling = [c for c in self.clients if c not in ready]
        for c in ready:
            if c.name in self._off_until:             # its cooldown just ended
                log.info("LLM: cooldown over, trying %s again", c.name)
                del self._off_until[c.name]
        if self.prefer:                               # routing: e.g. simple commands go to the small, fast model first
            ready.sort(key=lambda c: c.name != self.prefer)           # stable: everything else keeps its order
        return ready + cooling

    def generate(self, system: str, messages: list[dict], tools: list[Tool]) -> LLMResponse:
        last: Exception | None = None
        order = self._order()
        first_step = bool(messages) and messages[-1]["role"] == "user"
        for i, c in enumerate(order):
            try:
                t0 = self._clock()
                resp = c.generate(system, messages, tools)
                if first_step and not resp.text.strip() and not resp.tool_calls:
                    raise EmptyReply(f"{c.name} returned an empty reply")
                if resp.raw is not None:
                    resp.raw = (c.name, resp.raw)
                u = resp.usage or {}
                self.stats.append({"provider": c.name, "prompt": u.get("prompt"), "completion": u.get("completion"),
                                   "tools": len(tools), "seconds": self._clock() - t0})
                return resp
            except Exception as e:  # noqa: BLE001
                last = e
                nxt = order[i + 1] if i + 1 < len(order) else None
                if _provider_down(e) and nxt is not None:
                    wait = min(MAX_COOLDOWN, max(self.cooldown, retry_after(e) or 0.0))
                    self._off_until[c.name] = self._clock() + wait
                    log.warning("LLM switch: %s -> %s (%s). Staying on %s for %d s before trying %s again.",
                                c.name, nxt.name, _short(e), nxt.name, wait, c.name)
                else:
                    log.warning("LLM: %s failed (%s)%s", c.name, _short(e),
                                f"; using {nxt.name} for this request" if nxt else "")
        raise RuntimeError(f"All LLM providers failed: {last}")


def _short(e: BaseException) -> str:
    if isinstance(e, EmptyReply):
        return str(e)
    code = _status(e)
    return f"HTTP {code}" if code else f"{type(e).__name__}"


def build_client() -> FallbackClient:
    """Providers in the order given by [llm] order, using whichever keys are configured."""
    cfg = settings["llm"]
    specs = {
        "gemini": (GeminiClient, "GEMINI_API_KEY", "gemini_model", "gemini_rpm"),
        "groq": (GroqClient, "GROQ_API_KEY", "groq_model", "groq_rpm"),
        "groq-small": (GroqClient, "GROQ_API_KEY", "groq_small_model", "groq_rpm"),       # separate model = separate limits
    }
    wanted = []
    for name in cfg["order"]:
        if name not in specs:
            log.warning("[llm] order lists unknown provider %r; ignoring it", name)
            continue
        cls, key_name, model_key, rpm_key = specs[name]
        key, model = env(key_name), cfg[model_key]
        if key and not model:
            log.warning("%s is set but [llm] %s is empty in config.toml; skipping that provider", key_name, model_key)
        elif key:
            wanted.append((name, cls, model, key, cfg[rpm_key]))
    # with a fallback available, give up on a struggling provider quickly; with only one, be more patient
    solo = len(wanted) <= 1
    attempts = max(cfg["failover_attempts"], 4) if solo else cfg["failover_attempts"]
    fail_over_after = 20.0 if solo else RETRY_AFTER_FAILOVER
    clients: list[LLMClient] = []
    for name, cls, model, key, rpm in wanted:
        extra = {"reasoning_effort": cfg["groq_reasoning_effort"], "name": name} if cls is GroqClient else {}
        clients.append(cls(model, key, rpm, attempts, fail_over_after, **extra))
    return FallbackClient(clients, cooldown=cfg["failover_cooldown"])
