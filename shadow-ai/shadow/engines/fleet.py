"""Fleet Engine — several browsers, several goals, bounded concurrency.

A fleet is not "more autopilot". It is the acknowledgement that a person runs
several accounts in several profiles: one shop account checking out while
another tab of browsers are logged in somewhere else. Each worker gets its
own SHADOW context — its own engines, journal and CDP session — so nothing
shared is stepped on, and the only thing the workers have in common is the
scheduler that starts them.

The safety rules are inherited, not re-implemented: a worker refuses a
protected host exactly as a single run does, approvals are per worker, and a
worker that fails cannot take the fleet down with it.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..util import iso, new_id, now, truncate
from .base import Engine

# Two goals for different profiles at once, one goal for many profiles, or a
# single goal on a single profile — all of them are "one job per profile".
@dataclass
class FleetJob:
    goal: str
    profile: str
    id: str = field(default_factory=lambda: new_id("f-"))
    approved: bool = False
    status: str = "pending"          # pending | running | done | failed | needs_approval
    error: str = ""
    task_id: str = ""
    answer: Any = None
    started_at: float = 0.0
    ended_at: float = 0.0
    steps: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "profile": self.profile,
                "status": self.status, "error": truncate(self.error, 200),
                "task_id": self.task_id, "answer": self.answer, "steps": self.steps,
                "ms": int((self.ended_at - self.started_at) * 1000) if self.ended_at else 0,
                "started": iso(self.started_at) if self.started_at else ""}


class Fleet:
    """The runner. Kept out of the engine so it can be unit-tested on its own."""

    def __init__(self, worker_factory: Callable[[str], Any], max_parallel: int = 3,
                 printer: Optional[Callable[[str, str], None]] = None):
        self.worker_factory = worker_factory
        self.max_parallel = max(1, int(max_parallel))
        self.printer = printer or (lambda level, line: None)
        self.jobs: List[FleetJob] = []
        self._workers: Dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- one job -------------------------------------------------------
    def _worker(self, profile: str):
        with self._lock:
            brain = self._workers.get(profile)
            if brain is None:
                brain = self.worker_factory(profile)
                self._workers[profile] = brain
            return brain

    def _run_one(self, job: FleetJob) -> FleetJob:
        job.status = "running"
        job.started_at = now()
        self.printer("info", f"fleet: {job.profile} starts {truncate(job.goal, 50)}")
        try:
            brain = self._worker(job.profile)
            result = brain.run(job.goal, approved=job.approved)
            job.task_id = result.task_id
            job.status = result.status
            job.error = result.error or ""
            job.answer = result.results.get("answer")
            job.steps = len(result.steps)
        except Exception as exc:  # noqa: BLE001 - one worker never kills the fleet
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            job.ended_at = now()
        self.printer("ok" if job.status == "done" else "warn",
                     f"fleet: {job.profile} {job.status}")
        return job

    # -- many jobs -----------------------------------------------------
    def run(self, jobs: List[FleetJob]) -> List[FleetJob]:
        self.jobs.extend(jobs)
        if len(jobs) == 1:
            return [self._run_one(jobs[0])]
        workers = min(self.max_parallel, len(jobs))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fleet") as pool:
            list(pool.map(self._run_one, jobs))
        return jobs

    # -- background variant (a person keeps their prompt) ---------------
    def start(self, jobs: List[FleetJob]) -> str:
        run_id = new_id("run-")
        self.jobs.extend(jobs)
        thread = threading.Thread(target=self.run, args=(jobs,), daemon=True,
                                  name=f"fleet-{run_id}")
        thread.start()
        return run_id

    def close(self) -> None:
        with self._lock:
            for brain in self._workers.values():
                try:
                    brain.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self._workers.clear()

    def report(self) -> Dict[str, Any]:
        return {"jobs": [j.to_dict() for j in self.jobs],
                "parallel": self.max_parallel,
                "profiles": sorted(self._workers),
                "running": sum(1 for j in self.jobs if j.status == "running")}


class FleetEngine(Engine):
    """Engine wrapper: the fleet is addressable from a goal, the CLI and
    the dashboard without any of them owning threads."""

    name = "fleet"

    def __init__(self, ctx, factory: Optional[Callable[[str], Any]] = None):
        super().__init__(ctx)
        self._factory = factory
        self._fleet: Optional[Fleet] = None

    # -- construction --------------------------------------------------
    def set_factory(self, factory: Callable[[str], Any]) -> None:
        self._factory = factory
        if self._fleet is not None:
            self._fleet.worker_factory = factory

    @property
    def fleet(self) -> Fleet:
        if self._fleet is None:
            if self._factory is None:
                raise RuntimeError("the fleet has no worker factory; the runtime wires one at boot")
            self._fleet = Fleet(self._factory, max_parallel=self.ctx.cfg.fleet.max_parallel,
                                printer=self._line)
        return self._fleet

    def _line(self, level: str, text: str) -> None:
        ui = self.ctx.registry.get_or_none("ui")
        if ui is not None:
            ui.notify(level, text)
        self.log.info("%s", text)

    # -- verbs ---------------------------------------------------------
    def fan_out(self, goal: str, profiles: List[str], approved: bool = False) -> List[Dict[str, Any]]:
        """The same goal on several profiles, one browser each."""
        return self.run_jobs([FleetJob(goal=goal, profile=p, approved=approved) for p in profiles])

    def run_jobs(self, jobs: List[FleetJob], background: bool = False) -> List[Dict[str, Any]]:
        if not jobs:
            return []
        for job in jobs:
            # A protected host is refused before any worker is started, exactly
            # like a single run — the fleet inherits the rule, it never bypasses.
            safety = self.ctx.registry.get_or_none("safety")
            if safety is not None:
                from ..errors import ApprovalRequired
                try:
                    for url in _urls_in(job.goal):
                        safety.check_url(url, approved=job.approved)
                except ApprovalRequired as exc:
                    job.status = "needs_approval"
                    job.error = exc.message
        runnable = [j for j in jobs if j.status == "pending"]
        if background:
            run_id = self.fleet.start(runnable) if runnable else ""
            return [dict(j.to_dict(), run=r) for j, r in zip(jobs, [run_id] * len(jobs))]
        if runnable:
            if len({j.profile for j in runnable}) == 1 and len(runnable) > 1:
                # one profile, several goals: sequential on purpose, one browser
                for job in runnable:
                    self.fleet.run([job])
            else:
                self.fleet.run(runnable)
        return [j.to_dict() for j in jobs]

    def report(self) -> Dict[str, Any]:
        if self._fleet is None:
            return {"jobs": [], "parallel": self.ctx.cfg.fleet.max_parallel,
                    "profiles": [], "running": 0}
        return self._fleet.report()

    def close(self) -> None:
        if self._fleet is not None:
            self._fleet.close()

    def stop(self) -> None:
        self.close()
        super().stop()

    def health(self) -> Dict[str, Any]:
        info = super().health()
        report = self.report()
        info.update({"workers": len(report["profiles"]), "running": report["running"],
                     "jobs": len(report["jobs"]), "max_parallel": report["parallel"]})
        return info


_OPEN_URL = None


def _urls_in(goal: str) -> List[str]:
    from ..engines.intelligence import URL_RE
    return [m.group(1) for m in URL_RE.finditer(goal or "")]
