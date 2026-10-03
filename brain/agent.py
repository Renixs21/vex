"""Agent loop: LLM <-> tools, sequential, capped at max_steps.

Every tool call goes through the safety Executor (tiers, confirmations, audit, undo, dry-run); the agent never calls a
tool function directly. Per message it:
  * tells the executor what the USER said (administrator rights depend on the user's own words, see safety/elevation.py),
  * offers the model only a core set of tools plus the groups that match the message (see tools/categories.py) and refuses
    calls to tools it wasn't offered,
  * retrieves relevant memory into that turn's system prompt only,
  * cleans the final reply (invisible characters, leaked notes, markdown) before anyone sees or hears it.
"""
from __future__ import annotations

import logging

import tools
from brain.llm import FallbackClient, build_client
from brain.prompts import MEMORY_HEADER, SYSTEM_PROMPT
from brain.sanitize import clean_reply
from config import settings
from safety.executor import Executor, build_default_executor
from tools import categories, registry
from tools.registry import ToolResult

log = logging.getLogger("vex.agent")
MAX_HISTORY = 40


class Agent:
    def __init__(self, llm: FallbackClient | None = None, executor: Executor | None = None, memory=None):
        tools.load_all()
        self.llm = llm or build_client()
        self.executor = executor or build_default_executor()
        self.history: list[dict] = []
        self.max_steps: int = settings["llm"]["max_steps"]
        self.memory = memory
        self._carry: set[str] = set()          # tool groups used last turn stay on for the next one
        if memory is not None:
            from tools import memory as memory_tools

            memory_tools.configure(memory)           # the memory tools and the prompt context share one instance

    def _memory(self):
        if self.memory is None:
            try:
                from tools import memory as memory_tools

                self.memory = memory_tools.get_memory()
            except Exception:  # noqa: BLE001 - the assistant still works without memory
                log.exception("memory unavailable")
                self.memory = False
        return self.memory or None

    def system_prompt(self, user_text: str) -> str:
        """The system prompt plus the memory facts relevant to this message (kept out of the stored history)."""
        base = SYSTEM_PROMPT.replace("{categories}", categories.category_summary())
        mem = self._memory()
        if mem is None:
            return base
        try:
            text = mem.retrieve(user_text).text
        except Exception:  # noqa: BLE001
            log.exception("memory retrieval failed")
            return base
        if not text:
            return base
        text = text.replace("<memory>", "").replace("</memory>", "")      # facts can't close the data block
        return f"{base}\n{MEMORY_HEADER}\n<memory>\n{text}\n</memory>\n"

    def _trim(self) -> None:
        """Keep recent history, starting on a user turn so tool results never lose their call."""
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
            while self.history and self.history[0]["role"] != "user":
                self.history.pop(0)

    def run(self, user_text: str) -> str:
        """Handle one user message and return Vex's final reply."""
        elevation = getattr(self.executor, "elevation", None)
        if elevation is not None:
            elevation.begin_turn(user_text)               # only the user's own message can enable admin rights
        self.history.append({"role": "user", "text": user_text})
        self._trim()
        if hasattr(self.llm, "prefer"):                   # routing: simple one-tool commands go to the small, fast model first
            self.llm.prefer = "groq-small" if settings["llm"]["route_simple"] and categories.simple_command(user_text) else None
        system = self.system_prompt(user_text)
        active = categories.match(user_text) | self._carry
        requested: set[str] = set()                       # groups the model asked for with more_tools
        used: set[str] = set()
        try:
            for _ in range(self.max_steps):
                offered = registry.tools_for(active | requested) if settings["llm"]["tool_groups"] else registry.all_tools()
                offered_names = {t.name for t in offered}
                resp = self.llm.generate(system, self.history, offered)
                provider, raw = resp.raw if resp.raw else (None, None)
                text = resp.text if resp.tool_calls else clean_reply(resp.text)   # final replies are cleaned
                self.history.append({"role": "assistant", "text": text, "tool_calls": resp.tool_calls,
                                     "raw": raw, "raw_provider": provider})
                if not resp.tool_calls:
                    return text or "Done."
                for call in resp.tool_calls:
                    log.info("tool %s", call.name)
                    if call.name not in offered_names:
                        result = ToolResult(False, f"{call.name} isn't available right now. Call more_tools with its category "
                                                   f"({', '.join(categories.CATEGORIES)}) first.")
                    else:
                        result = self.executor.run(call.name, call.args)
                        tool = registry.get(call.name)
                        if tool is not None and tool.category != "core":
                            used.add(tool.category)
                        if call.name == "more_tools" and result.ok and call.args.get("category") in categories.CATEGORIES:
                            requested.add(call.args["category"])
                    log.info("  -> ok=%s", result.ok)
                    self.history.append({"role": "tool", "id": call.id, "name": call.name, "result": result.as_dict()})
            return "I hit my step limit before finishing. Try again or break it into smaller steps."
        finally:
            self._carry = used | requested
