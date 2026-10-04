"""Self-healing building blocks: a restart budget, and a watch over one component that restarts it a few times, then gives up out loud.

The same rule everywhere (Vex itself, the orb, the hotkey, the wake word): at most 3 restarts in 10 minutes. After that, stop trying,
say so once (with where to look), and leave the rest of Vex running. Endless restart loops are worse than a clear message.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

log = logging.getLogger("vex.recovery")

MAX_RESTARTS = 3
WINDOW_SECONDS = 600.0


class RestartBudget:
    """`allow()` is True up to `maximum` times within `window` seconds (each True use is counted)."""

    def __init__(self, maximum: int = MAX_RESTARTS, window: float = WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.maximum, self.window, self._clock = maximum, window, clock
        self._used: deque[float] = deque()

    def _trim(self) -> None:
        now = self._clock()
        while self._used and now - self._used[0] > self.window:
            self._used.popleft()

    def allow(self) -> bool:
        self._trim()
        if len(self._used) >= self.maximum:
            return False
        self._used.append(self._clock())
        return True

    @property
    def used(self) -> int:
        self._trim()
        return len(self._used)


class ComponentWatch:
    """`check()` (called periodically): if the component is unhealthy and `wanted()`, restart it within the budget; give up once, loudly."""

    def __init__(self, name: str, is_healthy: Callable[[], bool], restart: Callable[[], object], on_giveup: Callable[[str], None],
                 wanted: Callable[[], bool] = lambda: True, budget: RestartBudget | None = None):
        self.name, self.is_healthy, self.restart, self.on_giveup, self.wanted = name, is_healthy, restart, on_giveup, wanted
        self.budget = budget or RestartBudget()
        self.gave_up = False
        self.restarts = 0

    def check(self) -> str:
        """'ok' | 'restarted' | 'gave_up' | 'idle' (not wanted, or already given up)."""
        if self.gave_up or not self.wanted():
            return "idle"
        try:
            if self.is_healthy():
                return "ok"
        except Exception:  # noqa: BLE001
            log.exception("health check of %s failed", self.name)
            return "ok"
        if not self.budget.allow():
            self.gave_up = True
            log.error("%s keeps failing: not restarting it again (%d restarts in %.0f min)", self.name, self.budget.maximum, self.budget.window / 60)
            try:
                self.on_giveup(self.name)
            except Exception:  # noqa: BLE001
                log.exception("give-up notice for %s failed", self.name)
            return "gave_up"
        self.restarts += 1
        log.warning("%s stopped working; restarting it (%d in the last %.0f min)", self.name, self.budget.used, self.budget.window / 60)
        try:
            self.restart()
        except Exception:  # noqa: BLE001
            log.exception("restarting %s failed", self.name)
        return "restarted"
