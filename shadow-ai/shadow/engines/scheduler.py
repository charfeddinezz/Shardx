"""Scheduler — jobs that run later, or again, without a cron daemon.

A job is a function or a goal, with a repeat interval in seconds and an
optional run limit. The scheduler runs in its own thread, catches everything a
job raises, and reports to the bus so the Supervisor and the UI see it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..util import iso, new_id, now, truncate
from .base import Engine


@dataclass
class Job:
    name: str
    run: Callable[[], Any]
    every_s: float = 0.0
    at: Optional[float] = None
    limit: int = 0
    id: str = field(default_factory=lambda: new_id("job-"))
    runs: int = 0
    failures: int = 0
    next_at: float = 0.0
    last: str = ""
    paused: bool = False
    done: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "every_s": self.every_s, "runs": self.runs,
                "failures": self.failures, "paused": self.paused, "done": self.done,
                "next_at": iso(self.next_at) if self.next_at else None,
                "last": truncate(self.last, 160)}


class SchedulerEngine(Engine):
    name = "scheduler"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.jobs: Dict[str, Job] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        super().start()
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="shadow-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        super().stop()

    # -- jobs ---------------------------------------------------------
    def every(self, name: str, run: Callable[[], Any], seconds: float, limit: int = 0) -> Job:
        job = Job(name=name, run=run, every_s=max(0.05, float(seconds)), limit=limit,
                  next_at=now() + max(0.05, float(seconds)))
        with self._lock:
            self.jobs[job.id] = job
        self.note(name=name, every_s=job.every_s)
        self.emit("scheduled", job=job.id, name=name, in_s=job.every_s)
        return job

    def after(self, name: str, run: Callable[[], Any], seconds: float) -> Job:
        job = Job(name=name, run=run, at=now() + max(0.0, float(seconds)), next_at=now() + max(0.0, float(seconds)))
        with self._lock:
            self.jobs[job.id] = job
        self.emit("scheduled", job=job.id, name=name, in_s=job.at - now())
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            return self.jobs.pop(job_id, None) is not None

    def pause(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.paused = True
        return True

    def resume(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.paused = False
        job.next_at = now() + (job.every_s or 1.0)
        return True

    def pending(self) -> List[Job]:
        with self._lock:
            return [j for j in self.jobs.values() if not j.done and not j.paused]

    # -- loop ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            due = [j for j in self.pending() if j.next_at <= now()]
            for job in due:
                self._fire(job)
            self._stop.wait(0.25)

    def _fire(self, job: Job) -> None:
        started = time.time()
        try:
            result = job.run()
            job.last = truncate(str(result), 200) if result is not None else "ok"
            job.runs += 1
            self.emit("ran", job=job.id, name=job.name, ms=int((time.time() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - a job must not kill the scheduler
            job.failures += 1
            job.last = f"{type(exc).__name__}: {exc}"
            self.fail(exc, job=job.name)
            self.ctx.journal.append("job.failed", job.id, -1, name=job.name, error=job.last[:300])
        finally:
            if job.every_s and (not job.limit or job.runs < job.limit):
                job.next_at = now() + job.every_s
            else:
                job.done = True

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"jobs": len(self.jobs), "running": self._thread is not None and self._thread.is_alive()})
        return info
