"""Scheduler — jobs that run later, or again, without a cron daemon.

A job is a function or a goal, with a repeat interval in seconds and an
optional run limit. The scheduler runs in its own thread, catches everything a
job raises, and reports to the bus so the Supervisor and the UI see it.
"""

from __future__ import annotations

import json
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
    goal: str = ""          # set instead of `run` when a job is a saved goal

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
        self._loading = False     # writes are suspended while jobs are restored

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        super().start()
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="shadow-scheduler", daemon=True)
            self._thread.start()
        restored = self.load()
        if restored:
            self.emit("restored", jobs=restored)
            self.note(restored=restored)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        super().stop()

    # -- persistence ---------------------------------------------------
    def _path(self):
        return self.ctx.cfg.state_path() / "jobs.json"

    def save(self) -> int:
        """Write the jobs a person asked for, so a restart does not forget them.

        Jobs with a closure cannot be written; jobs added with `goal=` can, and
        those are the ones a person schedules and expects after a reboot.
        """
        if self._loading:
            return 0
        saved = []
        with self._lock:
            for job in self.jobs.values():
                if not job.goal or job.done:
                    continue
                saved.append({"id": job.id, "name": job.name, "goal": job.goal,
                              "every_s": job.every_s, "limit": job.limit,
                              "paused": job.paused, "runs": job.runs,
                              "failures": job.failures})
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        except OSError as exc:
            self.fail(exc, where="jobs.json")
            return 0
        return len(saved)

    def load(self) -> int:
        """Re-create the saved goals. Returns how many came back."""
        try:
            raw = self._path().read_text(encoding="utf-8")
        except (OSError, ValueError):
            return 0
        try:
            saved = json.loads(raw)
        except ValueError:
            return 0
        restored = 0
        known = set(self.jobs) | {job.id for job in self.jobs.values()}
        self._loading = True
        try:
            for item in saved if isinstance(saved, list) else []:
                goal = (item or {}).get("goal") or ""
                if not goal or (item.get("id") and item["id"] in known):
                    continue      # an engine restart must not double every job
                job = self.every_goal(item.get("name") or truncate(goal, 40), goal,
                                      float(item.get("every_s") or 0) or 60.0,
                                      limit=int(item.get("limit") or 0))
                with self._lock:
                    # re-key: a job's identity is the id written on disk, not
                    # the one it was born with
                    self.jobs.pop(job.id, None)
                    job.id = item.get("id") or job.id
                    job.paused = bool(item.get("paused"))
                    job.runs = int(item.get("runs") or 0)
                    job.failures = int(item.get("failures") or 0)
                    if job.paused:
                        job.next_at = 0
                    self.jobs[job.id] = job
                known.add(job.id)
                restored += 1
        finally:
            self._loading = False
        return restored

    def every_goal(self, name: str, goal: str, seconds: float, limit: int = 0) -> Job:
        """The scheduler's own use case: a goal, on a clock, written down."""
        def run():
            brain = getattr(self.ctx, "_run_goal", None)
            if brain is None:
                raise RuntimeError("no goal runner wired into this context")
            result = brain(goal)
            return getattr(result, "status", "done")
        job = self.every(name, run, seconds, limit=limit)
        job.goal = goal
        self.save()
        return job

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
            found = self.jobs.pop(job_id, None) is not None
        self.save()
        return found

    def pause(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.paused = True
        self.save()
        return True

    def resume(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job.paused = False
        job.next_at = now() + (job.every_s or 1.0)
        self.save()
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

    def compact_journal(self, keep_last: int = 5000) -> int:
        """The journal is append-only by design; something has to trim it."""
        dropped = self.ctx.journal.compact(keep_last=keep_last)
        if dropped:
            self.emit("journal_compacted", dropped=dropped)
        return dropped

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"jobs": len(self.jobs), "running": self._thread is not None and self._thread.is_alive()})
        return info
