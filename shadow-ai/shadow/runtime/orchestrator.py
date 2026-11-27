"""SHADOW AI — the assembled system.

    brain = ShadowAI(cfg)
    brain.boot()
    result = brain.run("Open shop.local, search for lamp, read the price of the desk")
    brain.shutdown()

Every engine is registered here, in dependency order, and the supervisor is
registered last so it can watch the rest. Nothing else in the package knows
the whole cast; this file is the only place that does.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.config import ShadowConfig
from ..core.context import ShadowContext
from ..core.events import EventBus
from ..core.journal import Journal
from ..core.registry import EngineState
from ..engines.actions import ActionsEngine
from ..engines.browser import BrowserEngine
from ..engines.computer import ComputerEngine
from ..engines.data import DataEngine
from ..engines.decision import DecisionEngine
from ..engines.discovery import DiscoveryEngine
from ..engines.input import KeyboardEngine, MouseEngine
from ..engines.intelligence import IntelligenceEngine
from ..engines.memory import MemoryEngine
from ..engines.page_understanding import PageUnderstandingEngine
from ..engines.pages import PagesEngine
from ..engines.react import ReasoningEngine
from ..engines.recovery import RecoveryEngine
from ..engines.safety import SafetyEngine
from ..engines.scheduler import SchedulerEngine
from ..engines.supervisor import AutonomousSupervisor
from ..engines.ui import UiEngine
from ..engines.vision import VisionEngine
from ..engines.workflow import Task, TaskResult, WorkflowEngine
from ..util import truncate

log = logging.getLogger("shadow")


class ShadowAI:
    def __init__(self, cfg: Optional[ShadowConfig] = None, printer=None, mock_launcher=None):
        self.cfg = cfg or ShadowConfig.load()
        _configure_logging(self.cfg.log_level)
        self.ctx = ShadowContext(self.cfg)
        self.ui = UiEngine(self.ctx, printer=printer)
        self.mock_launcher = mock_launcher
        self._built = False
        self._build()

    # -- assembly -----------------------------------------------------
    def _build(self) -> None:
        r = self.ctx.registry
        engines = [
            ("memory", MemoryEngine(self.ctx), ()),
            ("safety", SafetyEngine(self.ctx), ()),
            ("intel", IntelligenceEngine(self.ctx), ()),
            ("discovery", DiscoveryEngine(self.ctx), ()),
            ("page", PageUnderstandingEngine(self.ctx), ()),
            ("browser", BrowserEngine(self.ctx), ()),
            ("pages", PagesEngine(self.ctx), ("browser",)),
            ("keyboard", KeyboardEngine(self.ctx), ("browser",)),
            ("mouse", MouseEngine(self.ctx), ("browser",)),
            ("vision", VisionEngine(self.ctx), ()),
            ("data", DataEngine(self.ctx), ("memory",)),
            ("computer", ComputerEngine(self.ctx), ("safety",)),
            ("recovery", RecoveryEngine(self.ctx), ()),
            ("ui", self.ui, ()),
            ("decision", DecisionEngine(self.ctx), ("intel", "page")),
            ("actions", ActionsEngine(self.ctx), ("safety", "discovery", "keyboard", "mouse",
                                                  "page", "data", "memory", "browser", "pages")),
            ("react", ReasoningEngine(self.ctx), ("actions", "decision", "page", "safety",
                                                  "memory", "intel")),
            ("workflow", WorkflowEngine(self.ctx, ui=self.ui), ("intel", "memory", "safety", "browser",
                                                                "keyboard", "mouse", "discovery",
                                                                "page", "data", "recovery", "actions",
                                                                "react")),
            ("scheduler", SchedulerEngine(self.ctx), ()),
            ("supervisor", AutonomousSupervisor(self.ctx, on_resume=self._on_resume), ("workflow", "browser")),
        ]
        for name, engine, deps in engines:
            r.register(name, engine, depends_on=list(deps))
        # Every journal write passes the safety engine before it reaches disk.
        self.ctx.journal.scrub = lambda data: self.ctx.registry.get("safety").scrub_output(data)
        self._built = True

    # -- lifecycle ----------------------------------------------------
    def boot(self, engine_names: Optional[List[str]] = None) -> Dict[str, str]:
        report = self.ctx.registry.start_all(only=engine_names, bus=self.ctx.bus)
        failed = [n for n, s in report.items() if s != EngineState.READY]
        self.ctx.journal.append("runtime.boot", "-", -1, report=report, profile=self.cfg.default_profile,
                                mock=self.cfg.mock, version=_version())
        self.ui.notify("ok" if not failed else "warn",
                       f"SHADOW AI up: {len(report) - len(failed)}/{len(report)} engines"
                       + (f" · failed: {', '.join(failed)}" if failed else ""))
        return report

    def shutdown(self) -> None:
        self.ctx.cancel()
        self.ctx.registry.stop_all()
        self.ctx.close()
        log.info("shutdown complete")

    def __enter__(self) -> "ShadowAI":
        self.boot()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    # -- the main verbs -----------------------------------------------
    def run(self, goal: str, profile: str = "", resume: bool = True,
            approved: bool = False) -> TaskResult:
        """Understand a goal and carry it out, end to end."""
        self.ctx.reset_cancel()
        if profile:
            self.cfg.default_profile = profile
            self.ctx.profile_id = profile
        workflow: WorkflowEngine = self.ctx.registry.get("workflow")
        self.ui.notify("action", f"goal: {truncate(goal, 100)}")
        self.ctx.journal.append("goal", "-", -1, goal=goal, profile=self.ctx.profile_id)
        return workflow.run_goal(goal, resume=resume, profile=profile, approved=approved)

    def plan(self, goal: str) -> Task:
        return self.ctx.registry.get("workflow").plan(goal)

    def ask(self, question: str, url: str = "") -> Dict[str, Any]:
        """Read the current page (or `url`) and answer a question from it."""
        if url:
            self.ctx.registry.get("browser").navigate(url)
        probe = self.ctx.page()
        model = self.ctx.registry.get("page").understand(probe, self.ctx.cdp)
        return self.ctx.registry.get("page").answer(model, question)

    def read_page(self, url: str = "") -> Dict[str, Any]:
        if url:
            self.ctx.registry.get("browser").navigate(url)
        model = self.ctx.registry.get("page").understand(self.ctx.page(), self.ctx.cdp)
        return model.to_dict()

    def approve(self, task_id: str) -> TaskResult:
        workflow: WorkflowEngine = self.ctx.registry.get("workflow")
        task = workflow.current
        if task is None or task.id != task_id:
            # rebuild the paused task from the journal
            entries = self.ctx.journal.entries(task_id=task_id)
            started = next((e for e in entries if e.kind == "task.started"), None)
            if started is None:
                raise ValueError(f"no task {task_id!r} in the journal")
            task = self.plan((started.data or {}).get("goal", ""))
            task.id = task_id
            for step in task.steps:
                step.status = "pending"
        return workflow.approve(task)

    # -- introspection ------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "version": _version(),
            "context": self.ctx.snapshot(),
            "engines": self.ctx.registry.health(),
            "supervisor": self.ctx.registry.get("supervisor").report(),
            "journal": {"tasks": self.ctx.journal.task_ids()[-20:],
                        "task_count": len(self.ctx.journal.task_ids()),
                        "path": str(self.cfg.journal_path())},
        }

    def engine(self, name: str) -> Any:
        return self.ctx.registry.get(name)

    @property
    def bus(self) -> EventBus:
        return self.ctx.bus

    @property
    def journal(self) -> Journal:
        return self.ctx.journal

    # -- supervisor callback ------------------------------------------
    def _on_resume(self, task_id: str, last_step: int) -> None:
        """Re-run an interrupted task from the step after the last finished one."""
        entries = self.ctx.journal.entries(task_id=task_id)
        started = next((e for e in entries if e.kind == "task.started"), None)
        if started is None:
            return
        goal = (started.data or {}).get("goal", "")
        self.ui.notify("warn", f"resuming interrupted task {task_id} at step {last_step + 2}")
        workflow: WorkflowEngine = self.ctx.registry.get("workflow")
        if workflow.current is not None:
            return  # never run two tasks through one browser at once
        task = workflow.plan(goal)
        task.id = task_id
        for i, step in enumerate(task.steps):
            if i <= last_step:
                step.status = "done"
        workflow.run(task, resume=True)


def _configure_logging(level: str) -> None:
    root = logging.getLogger("shadow")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                               "%H:%M:%S"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))


def _version() -> str:
    from .. import __version__
    return __version__
