"""The engine contract.

An engine is a small object the Supervisor can treat uniformly:

    start()   -> None        bring dependencies up; idempotent
    stop()    -> None        release them; idempotent
    health()  -> dict        {"healthy": bool, ...} — cheap, never raises

Engines talk to the rest of the system through `self.ctx` (config, bus,
journal, registry, browser) and through the register of other engines, never
by importing each other.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from ..core.context import ShadowContext


class Engine:
    name = "engine"
    depends_on: tuple = ()

    def __init__(self, ctx: ShadowContext):
        self.ctx = ctx
        self.log = logging.getLogger(f"shadow.{self.name}")
        self.started_at: Optional[float] = None
        self._started = False
        self._stopped = False
        self.calls = 0
        self.errors = 0
        self.last_error = ""

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        self.started_at = time.time()
        self._started = True
        self._stopped = False
        self.log.debug("%s started", self.name)

    def stop(self) -> None:
        self._started = False
        self._stopped = True
        self.log.debug("%s stopped", self.name)

    def health(self) -> Dict[str, Any]:
        return {"healthy": self._started and not self._stopped,
                "calls": self.calls, "errors": self.errors,
                "last_error": self.last_error,
                "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0}

    # -- helpers ------------------------------------------------------
    def engine(self, name: str) -> Any:
        return self.ctx.registry.get(name)

    def emit(self, topic: str, **data: Any) -> None:
        if "source" in data:           # the envelope already has one
            data["origin"] = data.pop("source")
        self.ctx.bus.publish(f"engine.{self.name}.{topic}", source=self.name, **data)

    def note(self, **data: Any) -> None:
        self.calls += 1
        if data:
            self.emit("call", **data)

    def fail(self, exc: BaseException, **data: Any) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.emit("error", error=self.last_error, **data)

    def reset(self) -> None:
        """Put runtime counters back, keeping configuration. Supervisor uses this."""
        self.errors = 0
        self.last_error = ""
