"""Recovery Engine — classify a failure, then do the smallest thing that fixes it.

A recovery is a named strategy with a bounded number of attempts and a
backoff. The engine never retries forever: after the budget it raises
`EngineDown` or returns the last failure, and the Supervisor takes over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..errors import (AmbiguousTarget, ElementNotFound, EngineDown, ShadowError,
                      TransportError)
from ..util import Backoff, retryable
from .base import Engine


@dataclass
class Attempt:
    strategy: str
    ok: bool
    ms: int
    detail: str = ""


@dataclass
class RecoveryPlan:
    error: str
    strategies: List[str]
    attempts: List[Attempt] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].ok


class RecoveryEngine(Engine):
    name = "recovery"

    STRATEGIES = ("settle", "rewait", "reattach", "restart_engine", "reload_page", "rescan")

    def __init__(self, ctx):
        super().__init__(ctx)
        self.backoff = Backoff(base=0.4, cap=8.0)
        self.recoveries = 0
        self.history: List[RecoveryPlan] = []

    # -- classification ------------------------------------------------
    def classify(self, exc: BaseException) -> List[str]:
        if isinstance(exc, ElementNotFound):
            return ["settle", "rewait", "rescan"]
        if isinstance(exc, AmbiguousTarget):
            return ["rescan"]
        if isinstance(exc, TransportError):
            return ["reattach", "restart_engine", "reload_page"]
        if isinstance(exc, EngineDown):
            return ["restart_engine"]
        if retryable(exc):
            return ["rewait", "reattach"]
        code = getattr(exc, "code", "")
        if code == "cdp":
            message = str(exc).lower()
            if "not attached" in message or "session" in message and "closed" in message:
                return ["reattach"]
            return ["rewait", "settle"]
        return ["settle"]

    # -- actions -------------------------------------------------------
    def _do(self, name: str) -> Optional[Callable[[], Any]]:
        ctx = self.ctx
        if name == "settle":
            return lambda: ctx.cdp.settle() if ctx.cdp else None
        if name == "rewait":
            return lambda: time.sleep(self.backoff.delay(1))
        if name == "reattach":
            return lambda: (ctx.close_cdp(), ctx.ensure_cdp())
        if name == "restart_engine":
            return lambda: ctx.registry.restart("browser", bus=ctx.bus) if "browser" in ctx.registry else ctx.ensure_cdp()
        if name == "reload_page":
            def reload_page():
                cdp = ctx.ensure_cdp()
                cdp.call("Page.reload", {"ignoreCache": False}, timeout=30)
                cdp.wait_ready(timeout=30)
            return reload_page
        if name == "rescan":
            return lambda: ctx.probe.snapshot(limit=50)
        return None

    # -- entry point ---------------------------------------------------
    def recover(self, exc: BaseException, operation: Optional[Callable[[], Any]] = None,
                max_rounds: int = 2) -> RecoveryPlan:
        self.note(error=type(exc).__name__)
        strategies = self.classify(exc)
        plan = RecoveryPlan(error=f"{type(exc).__name__}: {exc}", strategies=strategies)
        for round_no in range(1, max_rounds + 1):
            for strategy in strategies:
                action = self._do(strategy)
                if action is None:
                    continue
                started = time.time()
                try:
                    action()
                    detail = "ok"
                    if operation is not None:
                        operation()
                        detail = "operation succeeded"
                    plan.attempts.append(Attempt(strategy, True, int((time.time() - started) * 1000), detail))
                    self.recoveries += 1
                    self.history.append(plan)
                    self.emit("recovered", strategy=strategy, error=plan.error[:120])
                    return plan
                except Exception as again:  # noqa: BLE001 - keep trying the next strategy
                    plan.attempts.append(Attempt(strategy, False, int((time.time() - started) * 1000),
                                                 f"{type(again).__name__}: {again}"[:200]))
            time.sleep(self.backoff.delay(round_no))
        self.history.append(plan)
        self.fail(exc, strategies=strategies)
        self.emit("unrecovered", error=plan.error[:160], attempts=len(plan.attempts))
        return plan

    def run(self, operation: Callable[[], Any], retries: int = 2) -> Any:
        """Call `operation`, recovering between attempts. Returns its result."""
        last: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                return operation()
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if attempt == retries:
                    break
                if not isinstance(exc, ShadowError):
                    raise
                self.recover(exc)
        raise last if last else EngineDown("recovery exhausted")

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"recoveries": self.recoveries, "unrecovered": self.errors})
        return info
