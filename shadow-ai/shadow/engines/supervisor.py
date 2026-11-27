"""Autonomous Supervisor — watch every engine, catch the fall, resume the work.

This is the piece that makes the system unattended. One thread, one loop:

1. **heartbeat** — ask each engine `health()`. "Health" is a real probe where
   the engine can afford one (the browser reads a URL), not a remembered flag.
2. **detect** — an engine that is unhealthy, or that failed to answer within
   the heartbeat timeout, is marked down. So is a task that stopped making
   progress without ending.
3. **restart** — a down engine is restarted through the registry, with
   backoff and a cap inside a rolling window. An engine that keeps dying is
   quarantined instead of restart-looping, and the fact is journalled.
4. **resume** — when a task was interrupted (crash, kill, restart), the
   journal says which step finished last; the supervisor re-runs the task
   from the next step, once, and only if the profile is healthy.

Everything it does is an event, so a terminal or a dashboard can watch it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.registry import EngineState
from ..util import iso, now, truncate
from .base import Engine


@dataclass
class Incident:
    kind: str
    subject: str
    detail: str
    ts: float = field(default_factory=now)
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "detail": truncate(self.detail, 200),
                "ts": iso(self.ts), "resolved": self.resolved}


class AutonomousSupervisor(Engine):
    name = "supervisor"

    def __init__(self, ctx, on_resume: Optional[Callable[[str, int], Any]] = None):
        super().__init__(ctx)
        self.cfg = ctx.cfg.supervisor
        self.on_resume = on_resume
        self.incidents: List[Incident] = []
        self.restart_log: Dict[str, List[float]] = {}
        self.quarantined: Dict[str, str] = {}
        self.heartbeats = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.resumed_tasks: List[str] = []

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        super().start()
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="shadow-supervisor", daemon=True)
            self._thread.start()
        self.emit("watching")

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self.cfg.interval_s + 1))
        super().stop()

    # -- the loop -----------------------------------------------------
    def _loop(self) -> None:
        # first pass immediately, then at the configured interval
        while not self._stop.is_set():
            started = time.time()
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - the supervisor must never die
                self.fail(exc)
                self.ctx.journal.append("supervisor.error", "-", -1, error=str(exc)[:300])
            elapsed = time.time() - started
            self._stop.wait(max(0.05, self.cfg.interval_s - elapsed))

    def check_once(self) -> Dict[str, Any]:
        """One full pass. Public so tests and the CLI can drive it directly."""
        self.heartbeats += 1
        report: Dict[str, Any] = {"at": iso(), "restarted": [], "quarantined": [],
                                  "failing": [], "resumed": []}
        with self._lock:
            health = self.ctx.registry.health()
        for name, entry in health.items():
            if name == self.name:
                continue
            if name in self.quarantined:
                continue
            healthy = entry.get("healthy", True)
            state = entry.get("state")
            if healthy and state == EngineState.READY:
                continue
            if state in (EngineState.STOPPED, EngineState.NEW):
                continue  # never started, or stopped on purpose by an operator
            if state == EngineState.STARTING and not entry.get("starts"):
                continue  # registered while the system was already up: not watched yet
            detail = entry.get("last_error") or (entry.get("probe") or {}).get("error") or "unhealthy"
            self._incident("engine", name, str(detail))
            if self._restart(name):
                report["restarted"].append(name)
            elif name in self.quarantined:
                report["quarantined"].append(name)
            else:
                report["failing"].append(name)
        resumed = self.resume_interrupted()
        report["resumed"] = resumed
        self.ctx.bus.publish("supervisor.pass", source=self.name,
                             restarted=report["restarted"], quarantined=report["quarantined"],
                             resumed=resumed)
        return report

    # -- restart policy -----------------------------------------------
    def _restart(self, name: str) -> bool:
        """Restart an engine, obeying the cap inside the window.

        A restart that itself fails counts as an attempt. When the cap is
        reached the engine is quarantined: a component that dies five times
        in five minutes is not going to be fixed by a sixth try, and a
        restart loop would hide the real fault.
        """
        recent = [t for t in self.restart_log.get(name, []) if now() - t < self.cfg.restart_window_s]
        if len(recent) >= self.cfg.max_restarts:
            self._quarantine(name, f"{len(recent)} restarts inside {self.cfg.restart_window_s:.0f}s")
            return False
        delay = min(8.0, 0.25 * (2 ** len(recent)))
        time.sleep(delay)
        try:
            self.ctx.registry.restart(name, bus=self.ctx.bus)
        except Exception as exc:  # noqa: BLE001 - the restart is what failed
            recent.append(now())
            self.restart_log[name] = recent
            self.fail(exc, engine=name)
            self._incident("restart_failed", name, f"{type(exc).__name__}: {exc}")
            self.ctx.journal.append("supervisor.restart_failed", "-", -1, engine=name,
                                    error=str(exc)[:300])
            self.ctx.bus.publish("supervisor.restart_failed", source=self.name, engine=name,
                                 error=str(exc)[:200])
            if len(recent) >= self.cfg.max_restarts:
                self._quarantine(name, f"restart kept failing: {exc}")
            return False
        recent.append(now())
        self.restart_log[name] = recent
        self._incident("restart", name, f"restarted after {delay:.2f}s")
        self.ctx.journal.append("supervisor.restart", "-", -1, engine=name, delay_s=delay)
        self.ctx.bus.publish("supervisor.restart", source=self.name, engine=name, delay_s=delay)
        return True

    def _quarantine(self, name: str, why: str) -> None:
        self.quarantined[name] = why
        self.ctx.registry.record(name).state = EngineState.FAILED
        incident = Incident("quarantine", name, why)
        self.incidents.append(incident)
        self.ctx.journal.append("supervisor.quarantine", "-", -1, engine=name, why=why)
        self.ctx.bus.publish("supervisor.quarantine", source=self.name, engine=name, why=why)
        self.emit("quarantined", engine=name, why=why)

    def release(self, name: str) -> bool:
        """Put a quarantined engine back under watch after a human fixes it."""
        if name not in self.quarantined:
            return False
        del self.quarantined[name]
        self.restart_log.pop(name, None)
        try:
            self.ctx.registry.restart(name, bus=self.ctx.bus)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, engine=name)
            return False
        return True

    # -- resume -------------------------------------------------------
    def resume_interrupted(self) -> List[str]:
        """Find tasks that never ended in the journal and re-run them from their last step."""
        if not self.cfg.resume_tasks or self.on_resume is None:
            return []
        resumed: List[str] = []
        journal = self.ctx.journal
        for task_id in journal.task_ids():
            if task_id in self.resumed_tasks or task_id in ("-", ""):
                continue
            entries = journal.entries(task_id=task_id)
            kinds = {e.kind for e in entries}
            if "task.ended" in kinds or "task.started" not in kinds:
                continue
            if "task.paused" in kinds:
                continue  # waiting for a human, not interrupted
            if any(e.kind == "supervisor.resumed" for e in entries):
                continue
            workflow = self.ctx.registry.get("workflow") if "workflow" in self.ctx.registry else None
            current = getattr(workflow, "current", None)
            if current is not None and current.id == task_id:
                continue  # it is running, not interrupted
            newest = max((e.ts for e in entries), default=0.0)
            if now() - newest < self.cfg.heartbeat_timeout_s:
                continue  # the engine may still be working on it
            last = max((e.step for e in entries if e.kind in ("step.done", "step.started")), default=-1)
            started = (entries[0].data or {}).get("goal", "")
            self.resumed_tasks.append(task_id)
            self.ctx.journal.append("supervisor.resumed", task_id, last, goal=started)
            self.ctx.bus.publish("supervisor.resume", source=self.name, task=task_id, step=last)
            self.emit("resumed", task=task_id, step=last)
            try:
                self.on_resume(task_id, last)
                resumed.append(task_id)
            except Exception as exc:  # noqa: BLE001
                self.fail(exc, task=task_id)
        return resumed

    # -- reporting ----------------------------------------------------
    def _incident(self, kind: str, subject: str, detail: str) -> None:
        with self._lock:
            self.incidents.append(Incident(kind, subject, detail))
            if len(self.incidents) > 200:
                self.incidents = self.incidents[-200:]

    def report(self) -> Dict[str, Any]:
        health = self.ctx.registry.health()
        return {
            "heartbeats": self.heartbeats,
            "watching": self._thread is not None and self._thread.is_alive(),
            "engines": {name: {"healthy": e.get("healthy"), "state": e.get("state"),
                               "restarts": e.get("restarts", 0)}
                        for name, e in health.items()},
            "quarantined": dict(self.quarantined),
            "incidents": [i.to_dict() for i in self.incidents[-20:]],
            "resumed_tasks": list(self.resumed_tasks),
        }

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"heartbeats": self.heartbeats,
                     "watching": self._thread is not None and self._thread.is_alive(),
                     "quarantined": len(self.quarantined),
                     "incidents": len(self.incidents)})
        return info
