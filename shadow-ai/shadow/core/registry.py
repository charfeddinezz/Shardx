"""Engine registry and lifecycle.

The Supervisor needs one place that knows every engine: its state, the
function that brings it up, the function that checks it is alive, and the
order to start them in. Engines are plain objects with `name`, `start()`,
`stop()` and `health()`; anything that satisfies that shape can be registered,
including engines you write yourself.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import ConfigError, EngineDown
from ..util import iso, now


class EngineState:
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class EngineRecord:
    name: str
    engine: Any
    depends_on: List[str] = field(default_factory=list)
    state: str = EngineState.NEW
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    restarts: int = 0
    failures: int = 0
    last_error: str = ""
    last_health: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "depends_on": self.depends_on,
            "restarts": self.restarts,
            "failures": self.failures,
            "started_at": iso(self.started_at) if self.started_at else None,
            "stopped_at": iso(self.stopped_at) if self.stopped_at else None,
            "last_error": self.last_error,
            "health_age_s": (round(now() - self.last_health, 1) if self.last_health else None),
            "healthy": self.state == EngineState.READY,
            "starts": self.restarts + (1 if self.started_at else 0),
        }


class Registry:
    def __init__(self, bus=None):
        self._lock = threading.RLock()
        self._records: Dict[str, EngineRecord] = {}
        self._bus = bus

    # -- registration -------------------------------------------------
    def register(self, name: str, engine: Any, depends_on: Optional[List[str]] = None,
                 replace: bool = False) -> EngineRecord:
        with self._lock:
            if name in self._records and not replace:
                raise ConfigError(f"engine {name!r} is already registered")
            for dep in depends_on or []:
                if dep == name:
                    raise ConfigError(f"engine {name!r} cannot depend on itself")
            rec = EngineRecord(name=name, engine=engine, depends_on=list(depends_on or []))
            self._records[name] = rec
        return rec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._records.pop(name, None)

    def get(self, name: str) -> Any:
        rec = self._records.get(name)
        if rec is None:
            raise ConfigError(f"no engine named {name!r}")
        return rec.engine

    def record(self, name: str) -> EngineRecord:
        rec = self._records.get(name)
        if rec is None:
            raise ConfigError(f"no engine named {name!r}")
        return rec

    def names(self) -> List[str]:
        return list(self._records)

    def records(self) -> List[EngineRecord]:
        return list(self._records.values())

    def get_or_none(self, name: str) -> Any:
        """For tests and optional wiring: an engine that may not be registered."""
        rec = self._records.get(name)
        return rec.engine if rec is not None else None

    def __contains__(self, name: str) -> bool:
        return name in self._records

    # -- dependency order ---------------------------------------------
    def order(self) -> List[str]:
        """Topological order; ties broken by registration order."""
        seen: Dict[str, int] = {}
        out: List[str] = []
        order_keys = {n: i for i, n in enumerate(self._records)}

        def visit(name: str, stack: tuple) -> None:
            if name in seen:
                if seen[name] == 1:
                    raise ConfigError(f"dependency cycle through {name!r}")
                return
            if name not in self._records:
                raise ConfigError(f"engine {name!r} depends on unknown {name!r}")
            seen[name] = 1
            deps = sorted(self._records[name].depends_on, key=lambda d: order_keys.get(d, 0))
            for dep in deps:
                visit(dep, stack + (name,))
            seen[name] = 2
            out.append(name)

        for name in sorted(self._records, key=lambda n: order_keys[n]):
            visit(name, ())
        return out

    # -- calling engine methods ---------------------------------------
    def call(self, name: str, method: str, *args, **kwargs) -> Any:
        """Invoke `engine.<method>()`, awaiting it when it is a coroutine."""
        engine = self.get(name)
        fn = getattr(engine, method, None)
        if fn is None:
            raise ConfigError(f"engine {name!r} has no {method}()")
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            from .asyncio_util import run_sync
            return run_sync(result)
        return result

    def start_all(self, only: Optional[List[str]] = None, bus=None) -> Dict[str, str]:
        order = self.order()
        if only:
            keep = set(only)
            order = [n for n in order if n in keep or any(n in self._records[d].depends_on for d in keep)]
        report: Dict[str, str] = {}
        for name in order:
            rec = self._records[name]
            rec.state = EngineState.STARTING
            try:
                self.call(name, "start")
                rec.state = EngineState.READY
                rec.started_at = now()
                report[name] = EngineState.READY
                self._emit(bus, "engine.started", name=name)
            except Exception as exc:
                rec.state = EngineState.FAILED
                rec.failures += 1
                rec.last_error = f"{type(exc).__name__}: {exc}"
                report[name] = EngineState.FAILED
                self._emit(bus, "engine.failed", name=name, error=rec.last_error)
        return report

    def stop_all(self) -> Dict[str, str]:
        report: Dict[str, str] = {}
        for name in reversed(self.order()):
            rec = self._records[name]
            rec.state = EngineState.STOPPING
            try:
                self.call(name, "stop")
                rec.state = EngineState.STOPPED
                rec.stopped_at = now()
                report[name] = EngineState.STOPPED
            except Exception as exc:
                rec.state = EngineState.FAILED
                rec.last_error = f"{type(exc).__name__}: {exc}"
                report[name] = EngineState.FAILED
        return report

    def health(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for name, rec in self._records.items():
            entry = rec.to_dict()
            try:
                probe = getattr(rec.engine, "health", None)
                if probe is not None:
                    info = probe()
                    if inspect.isawaitable(info):
                        from .asyncio_util import run_sync
                        info = run_sync(info)
                    if isinstance(info, dict):
                        entry["probe"] = info
                        healthy = info.get("healthy", True)
                        entry["healthy"] = bool(healthy) and rec.state == EngineState.READY
                        if healthy and rec.state == EngineState.READY:
                            rec.last_health = now()
                    else:
                        entry["healthy"] = bool(info) and rec.state == EngineState.READY
            except Exception as exc:
                entry["healthy"] = False
                entry["probe"] = {"error": f"{type(exc).__name__}: {exc}"}
            out[name] = entry
        return out

    def restart(self, name: str, bus=None) -> None:
        rec = self.record(name)
        try:
            self.call(name, "stop")
        except Exception:
            pass
        self.call(name, "start")
        rec.state = EngineState.READY
        rec.restarts += 1
        rec.started_at = now()
        rec.last_error = ""
        rec.last_health = now()
        self._emit(bus, "engine.restarted", name=name, restarts=rec.restarts)

    @staticmethod
    def _emit(bus, topic: str, **data) -> None:
        if bus is not None:
            try:
                bus.publish(topic, source="registry", **data)
            except Exception:
                pass


def check_dependencies(registry: Registry, name: str) -> None:
    rec = registry.record(name)
    for dep in rec.depends_on:
        if registry.record(dep).state != EngineState.READY:
            raise EngineDown(f"{name} needs {dep}, which is {registry.record(dep).state}")
