"""Workflow Engine — a goal becomes steps, steps become actions, actions are logged.

The planner turns an `Interpretation` into a short, readable list of steps.
The engine executes them one at a time through the other engines, and after
every step it writes to the journal and to the memory engine's checkpoint, so
a run that dies at step 4 resumes at step 4 rather than at the top.

Steps are resolved at run time, against the page as it actually is: "click the
Sign in button" is one step, not a guess about a selector made before the page
was seen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import (AmbiguousTarget, ApprovalRequired, ElementNotFound,
                      ShadowError, StepFailed, TaskAborted)


def _wrap(exc: ShadowError, index: int) -> StepFailed:
    """Re-raise an engine error as a step failure without duplicating `message`."""
    details = {k: v for k, v in exc.to_dict().items() if k not in ("message", "step")}
    return StepFailed(exc.message, step=index, **details)
from ..util import iso, new_id, now, redact_obj, truncate
from .base import Engine

MAX_TEXT = 1200


@dataclass
class Step:
    action: str
    target: str = ""
    value: str = ""
    expect: str = ""
    optional: bool = False
    id: str = field(default_factory=lambda: new_id("s-"))
    status: str = "pending"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "action": self.action, "target": truncate(self.target, 120),
                "value": truncate(self.value, 80), "expect": truncate(self.expect, 80),
                "optional": self.optional, "status": self.status, "detail": truncate(self.detail, 200)}

    def label(self) -> str:
        bits = [self.action]
        if self.target:
            bits.append(repr(truncate(self.target, 40)))
        if self.value:
            bits.append("=" + repr(truncate(self.value, 30)))
        return " ".join(bits)


@dataclass
class TaskResult:
    task_id: str
    goal: str
    status: str
    steps: List[Step] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: float = field(default_factory=now)
    ended_at: float = 0.0
    resumed_from: int = -1
    approvals: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def to_dict(self) -> Dict[str, Any]:
        return {"task_id": self.task_id, "goal": self.goal, "status": self.status,
                "ok": self.ok, "steps": [s.to_dict() for s in self.steps],
                "results": redact_obj(self.results), "error": self.error,
                "started_at": iso(self.started_at),
                "ended_at": iso(self.ended_at) if self.ended_at else None,
                "duration_s": round((self.ended_at or now()) - self.started_at, 2),
                "resumed_from": self.resumed_from, "approvals": self.approvals}


@dataclass
class Task:
    goal: str
    steps: List[Step] = field(default_factory=list)
    profile: str = ""
    approved: bool = False
    id: str = field(default_factory=lambda: new_id("t-"))
    results: Dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    steps_done: int = 0
    status: str = "pending"
    approvals: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "profile": self.profile, "status": self.status,
                "steps": [s.to_dict() for s in self.steps], "results": redact_obj(self.results)}


class PlanBuilder:
    """Interpretation → steps. Pure, so the plan can be printed and reviewed."""

    def build(self, interpretation, page_kind: str = "generic") -> List[Step]:
        steps: List[Step] = []
        for intent in interpretation.intents:
            steps.extend(self._steps_for(intent, interpretation))
        if not steps:
            steps.append(Step("read", target="page", value=interpretation.goal))
        steps.append(Step("finish"))
        return _merge(steps)

    def _steps_for(self, intent, interpretation) -> List[Step]:
        kind = intent.kind
        if kind == "open":
            return [Step("goto", target=intent.target, expect="loaded")]
        if kind == "find":
            return [Step("find", target=intent.target, value=intent.target)]
        if kind == "fill":
            return [Step("fill", target=intent.target, value=intent.value)]
        if kind == "click":
            return [Step("click", target=intent.target)]
        if kind == "login":
            return [Step("login", target=intent.target, value=intent.value)]
        if kind == "submit":
            return [Step("submit", target=intent.target) if intent.target else Step("press", target="Enter")]
        if kind == "read":
            target = intent.target if intent.target not in ("", "page") else "page"
            return [Step("read", target=target, value=intent.value or interpretation.question)]
        if kind == "purchase":
            return [Step("approve", target=intent.target),
                    Step("click", target=_pay_label(intent.target), optional=True)]
        if kind == "scroll":
            return [Step("scroll", target=intent.target or "down")]
        if kind == "back":
            return [Step("back")]
        return [Step("read", target="page")]


def _pay_label(text: str) -> str:
    for candidate in ("Pay now", "Place order", "Complete order", "Checkout", "Buy now"):
        if candidate.lower() in (text or "").lower():
            return candidate
    return "Checkout"


def _merge(steps: List[Step]) -> List[Step]:
    """Drop a submit step that the click before it already covers."""
    out: List[Step] = []
    for step in steps:
        if (out and step.action == "submit" and out[-1].action == "click"
                and step.target.lower() in (out[-1].target or "").lower()):
            continue
        if out and out[-1].action == step.action and out[-1].target == step.target and step.action == "goto":
            continue
        out.append(step)
    return out


class WorkflowEngine(Engine):
    name = "workflow"
    depends_on = ("intel",)

    def __init__(self, ctx, ui=None):
        super().__init__(ctx)
        self.planner = PlanBuilder()
        self.ui = ui
        self.tasks_run = 0
        self.current: Optional[Task] = None

    # -- public -------------------------------------------------------
    def plan(self, goal: str, page_kind: str = "generic") -> Task:
        intel = self.engine("intel")
        interpretation = intel.understand(goal)
        steps = self.planner.build(interpretation, page_kind)
        task = Task(goal=goal, steps=steps, profile=self.ctx.profile_id)
        self.emit("planned", task=task.id, steps=len(steps),
                  plan=[s.label() for s in steps])
        return task

    def run_goal(self, goal: str, resume: bool = True, profile: str = "",
                 approved: bool = False) -> TaskResult:
        if profile:
            self.ctx.profile_id = profile
        task = self.plan(goal)
        task.approved = approved
        return self.run(task, resume=resume)

    def run(self, task: Task, resume: bool = True) -> TaskResult:
        self.tasks_run += 1
        self.current = task
        task.started_at = now()
        task.status = "running"
        result = TaskResult(task_id=task.id, goal=task.goal, status="running",
                            steps=task.steps, results=task.results)
        self.ctx.journal.append("task.started", task.id, -1, goal=task.goal,
                                profile=task.profile, steps=[s.to_dict() for s in task.steps])
        self.ctx.bus.publish("task.started", source=self.name, task=task.id,
                             goal=truncate(task.goal, 120), steps=len(task.steps))
        self._banner(task)

        start_index = 0
        if resume:
            point = self.engine("memory").resume_point(task.id)
            if point and point.get("step", -1) >= 0:
                start_index = int(point["step"]) + 1
                result.resumed_from = start_index - 1
                self._line("info", f"resuming at step {start_index + 1}/{len(task.steps)} from the journal")
        task.results.setdefault("history", [])

        try:
            for index in range(start_index, len(task.steps)):
                if self.ctx.cancelled.is_set():
                    raise TaskAborted("task was cancelled")
                budget = self.engine("safety").budget(task)
                if budget:
                    raise StepFailed(budget, step=index)
                step = task.steps[index]
                task.results["step"] = index
                self._run_step(task, step, index)
                task.steps_done += 1
                self.engine("memory").checkpoint(task.id, index, {"url": self._url()})
            result.status = "done"
            self._line("ok", f"done: {truncate(task.goal, 60)}")
        except ApprovalRequired as exc:
            result.status = "needs_approval"
            result.error = exc.message
            task.approvals.append(exc.to_dict())
            result.approvals = task.approvals
            self._line("warn", f"waiting for a human: {exc.message}")
            self.ctx.journal.append("task.paused", task.id, task.results.get("step", -1), **exc.to_dict())
        except TaskAborted as exc:
            result.status = "cancelled"
            result.error = exc.message
        except ShadowError as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self._line("error", result.error)
            self.ctx.journal.append("task.failed", task.id, task.results.get("step", -1), **exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - the run must end cleanly whatever happens
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self.fail(exc)
            self._line("error", result.error)
        finally:
            result.ended_at = now()
            task.status = result.status
            if result.ok:
                self.engine("memory").store.clear_checkpoint(task.id)
            self.ctx.journal.append("task.ended", task.id, task.results.get("step", -1),
                                    status=result.status, error=result.error)
            self.ctx.bus.publish("task.finished" if result.ok else "task.failed", source=self.name,
                                 task=task.id, status=result.status, error=result.error[:200])
            self.current = None
        return result

    def approve(self, task: Task, index: Optional[int] = None) -> TaskResult:
        """Re-run a paused task from the step that needed approval."""
        task.approved = True
        step_index = index if index is not None else int(task.results.get("step", 0))
        for i, step in enumerate(task.steps):
            if i >= step_index:
                step.status = "pending"
        self.engine("memory").checkpoint(task.id, step_index - 1, {"url": self._url()})
        return self.run(task, resume=True)

    # -- step dispatch -------------------------------------------------
    def _run_step(self, task: Task, step: Step, index: int) -> Any:
        step.status = "running"
        started = time.time()
        self._line("action", f"[{index + 1}/{len(task.steps)}] {step.label()}")
        self.ctx.journal.append("step.started", task.id, index, action=step.action,
                                target=step.target, value=step.value)
        self.ctx.bus.publish("step.started", source=self.name, task=task.id, step=index,
                             action=step.action, target=truncate(step.target, 80))
        try:
            output = self._do(task, step, index)
        except ApprovalRequired:
            step.status = "needs_approval"
            raise
        except (ElementNotFound, AmbiguousTarget) as exc:
            if step.optional:
                step.status = "skipped"
                step.detail = exc.message
                self._line("warn", f"skipped (optional): {exc.message[:70]}")
                self.ctx.journal.append("step.skipped", task.id, index, action=step.action,
                                        error=exc.message)
                return None
            plan = self.engine("recovery").recover(exc)
            if plan.recovered:
                output = self._do(task, step, index)
                self._line("warn", "recovered: " + ", ".join(a.strategy for a in plan.attempts))
                self.ctx.journal.append("step.recovered", task.id, index, action=step.action,
                                        strategies=[a.strategy for a in plan.attempts])
            else:
                step.status = "failed"
                step.detail = exc.message
                raise _wrap(exc, index) from None
        except ShadowError as exc:
            plan = self.engine("recovery").recover(exc)
            if plan.recovered and getattr(exc, "retryable", False):
                output = self._do(task, step, index)
                self.ctx.journal.append("step.recovered", task.id, index, action=step.action,
                                        strategies=[a.strategy for a in plan.attempts])
            else:
                step.status = "failed"
                step.detail = exc.message
                raise _wrap(exc, index) from None
        step.status = "done"
        elapsed = int((time.time() - started) * 1000)
        step.detail = truncate(_summarise(output), 200)
        task.results.setdefault("outputs", []).append({"step": index, "action": step.action,
                                                       "output": _summarise(output)})
        self.ctx.journal.append("step.done", task.id, index, action=step.action,
                                ms=elapsed, output=_summarise(output))
        self.ctx.bus.publish("step.done", source=self.name, task=task.id, step=index,
                             action=step.action, ms=elapsed)
        if output is not None and step.action in ("read", "extract"):
            task.results["answer"] = output if isinstance(output, str) else str(output)
        return output

    def _do(self, task: Task, step: Step, index: int) -> Any:
        ctx = self.ctx
        safety = self.engine("safety")
        keyboard = self.engine("keyboard")
        mouse = self.engine("mouse")
        discovery = self.engine("discovery")
        page = self.engine("page")
        data = self.engine("data")
        memory = self.engine("memory")
        action = step.action

        if action == "goto":
            verdict = safety.check_url(step.target)
            safety.enforce(verdict)
            cdp = ctx.ensure_cdp()
            cdp.navigate(step.target)
            return {"url": cdp.current_url(), "title": cdp.title()}

        if action == "finish":
            return {"finished": True}

        if action == "wait":
            time.sleep(float(step.value or 1.0))
            return {"waited_s": float(step.value or 1.0)}

        if action == "back":
            cdp = ctx.ensure_cdp()
            cdp.evaluate("history.back()")
            cdp.wait_ready()
            return {"url": cdp.current_url()}

        if action == "verify_loaded":
            cdp = ctx.ensure_cdp()
            return {"loaded": cdp.wait_ready(timeout=30), "url": cdp.current_url()}

        probe = ctx.page()
        model = page.understand(probe, ctx.cdp)
        url = model.url

        if action == "find":
            field = discovery.find_field(probe, "search")
            if field is not None:
                keyboard.fill_field(probe, field.node, step.target)
                keyboard.press("Enter")
                ctx.cdp.wait_ready()
                return {"searched": step.target, "url": ctx.cdp.current_url()}
            candidate = discovery.pick_or_none(probe, step.target)
            if candidate is not None:
                mouse.click_element(probe, candidate.node)
                ctx.cdp.wait_ready()
                return {"clicked": candidate.label, "url": ctx.cdp.current_url()}
            raise ElementNotFound(f"no search field and no link matching {step.target!r}")

        if action == "fill":
            field = discovery.find_field(probe, step.target)
            if field is None:
                raise ElementNotFound(f"no field matching {step.target!r}")
            sensitive = safety.check_action("fill", target=step.target, value=step.value)
            filled = keyboard.fill_field(probe, field.node, step.value)
            if filled.get("verified") is False:
                raise StepFailed(f"field {step.target!r} did not keep the value", step=index)
            memory.learn(url, "field:" + step.target, field.node.get("selector"))
            return {"field": field.node.get("selector"), "verified": filled.get("verified"),
                    "sensitive": bool(sensitive)}

        if action == "login":
            user = step.target or memory.recall("user", namespace="credentials", default="")
            password = step.value or memory.recall("password", namespace="credentials", default="")
            if not user or not password:
                raise ElementNotFound("login needs a user and a password: "
                                      "'log in with user: X pass: Y'")
            safety.check_action("login", target="login", value="password")
            user_field = discovery.find_field(probe, "username") or discovery.find_field(probe, "email")
            pass_field = discovery.find_field(probe, "password")
            if user_field is None or pass_field is None:
                raise ElementNotFound("this page has no username/password pair")
            keyboard.fill_field(probe, user_field.node, user)
            keyboard.fill_field(probe, pass_field.node, password)
            submit = discovery.pick_or_none(probe, "sign in button") or discovery.pick_or_none(probe, "log in button")
            if submit is not None:
                mouse.click_element(probe, submit.node)
            else:
                keyboard.press("Enter")
            ctx.cdp.wait_ready()
            memory.remember("user", user, "credentials")
            memory.remember("password", safety.redact(password), "credentials")
            return {"logged_in": True, "url": ctx.cdp.current_url()}

        if action == "click":
            verdict = safety.check_action("click", target=step.target, url=url,
                                          approved=task.approved)
            candidate = discovery.pick(probe, step.target)
            mouse.click_element(probe, candidate.node)
            memory.learn(url, "click:" + step.target, candidate.selector)
            ctx.cdp.wait_ready()
            return {"clicked": candidate.label, "selector": candidate.selector,
                    "url": ctx.cdp.current_url()}

        if action == "submit":
            label = step.target or "submit"
            verdict = safety.check_action("submit", target=label, url=url,
                                          approved=task.approved)
            candidate = discovery.pick_or_none(probe, label)
            if candidate is None:
                candidate = discovery.pick_or_none(probe, "submit button")
            if candidate is not None:
                mouse.click_element(probe, candidate.node)
            else:
                keyboard.press("Enter")
            ctx.cdp.wait_ready()
            return {"submitted": True, "url": ctx.cdp.current_url()}

        if action == "press":
            return keyboard.press(step.target)

        if action == "scroll":
            return mouse.scroll(step.target or "down", int(step.value or 480))

        if action == "type":
            return keyboard.type(step.value or step.target)

        if action == "read":
            target = step.target
            if target in ("title", "page title", "heading"):
                return ctx.cdp.title() if ctx.cdp else ""
            if target == "url":
                return url
            if target in ("", "page", "text"):
                answer = page.answer(model, step.value or task.goal)
                return answer["answer"] or truncate(model.text, MAX_TEXT)
            if target.startswith(("#", ".", "[")) or " " in target:
                try:
                    text = probe.text(target)
                except Exception:
                    text = None
                if text:
                    return text
                candidate = discovery.pick_or_none(probe, target)
                if candidate is not None:
                    return candidate.node.get("text") or candidate.node.get("name")
                raise ElementNotFound(f"nothing to read at {target!r}")
            if target in ("price", "prices", "total", "amount"):
                prices = data.prices(probe)
                if prices:
                    return prices[0]["amount"]
            candidate = discovery.pick_or_none(probe, target)
            if candidate is not None:
                return candidate.node.get("text") or candidate.node.get("name")
            answer = page.answer(model, step.value or target)
            if answer["answer"]:
                return answer["answer"]
            return {"text": truncate(model.text, 400), "title": model.title}

        if action == "extract":
            if step.target in ("table", "tables"):
                return data.table(probe)
            if step.target in ("links", "link"):
                return data.links(probe)
            if step.target in ("fields", "form"):
                return data.fields(probe)
            return data.list_items(probe, step.target or "li")

        if action == "remember":
            memory.remember(step.target or "note", step.value, "task:" + task.id)
            return {"remembered": step.target}

        if action == "assert":
            return self._assert(probe, step)

        if action == "approve":
            if task.approved:
                return {"approved": True, "action": step.target}
            raise ApprovalRequired(f"the goal asks for an irreversible action: {truncate(step.target, 100)}",
                                   category="purchase", task=task.id)

        if action == "screenshot":
            vision = self.engine("vision")
            frame = vision.shot()
            return frame.to_dict()

        if action == "ocr":
            return self.engine("vision").ocr()

        raise StepFailed(f"unknown step action {action!r}", step=index)

    def _assert(self, probe, step: Step) -> Dict[str, Any]:
        if step.target.startswith(("#", ".", "[")):
            actual = probe.text(step.target)
            ok = bool(actual and (step.value in actual if step.value else actual))
            return {"assert": ok, "actual": truncate(actual or "", 200)}
        count = probe.count(step.target)
        return {"assert": count > 0, "count": count}

    # -- presentation --------------------------------------------------
    def _banner(self, task: Task) -> None:
        if self.ui is None:
            return
        self.ui.banner(f"SHADOW · {truncate(task.goal, 58)}",
                       f"task {task.id} · {len(task.steps)} steps · profile {task.profile or '-'}")

    def _line(self, level: str, text: str) -> None:
        if self.ui is not None:
            self.ui.notify(level, text)
        else:
            self.log.info("%s", text)

    def _url(self) -> str:
        try:
            return self.ctx.cdp.current_url() if self.ctx.cdp else ""
        except Exception:
            return ""

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"tasks_run": self.tasks_run,
                     "current": self.current.id if self.current else None})
        return info


def _summarise(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return truncate(output, MAX_TEXT)
    if isinstance(output, dict):
        try:
            import json
            return truncate(json.dumps(output, ensure_ascii=False, default=str), MAX_TEXT)
        except Exception:
            return truncate(str(output), MAX_TEXT)
    return truncate(str(output), MAX_TEXT)
