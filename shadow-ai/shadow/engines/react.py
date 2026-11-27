"""Reasoning Engine — look at the page, decide one thing, do it, look again.

This is the loop a plan cannot be. A plan is written before the page is seen;
this engine re-reads the page after every action and decides the next one from
what is actually there. It stops when any of these is true:

* the goal looks answered (a read step produced text that satisfies it);
* the page has not changed for `react.stale_after` actions and nothing is left
  to do — that is a stuck run, and it ends rather than spinning;
* `react.max_turns` actions have been taken;
* Safety raises, or a step needs a person.

Three sources, in order: the local model when one is configured and its
proposal survives validation; the plan's next not-yet-run step; and finally a
page-driven guess. Every choice is journaled with the reason it was made, so a
run can be read afterwards and understood.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import (AmbiguousTarget, ApprovalRequired, ElementNotFound,
                      ShadowError, TaskAborted)
from ..util import iso, new_id, now, truncate
from .base import Engine
from .workflow import Step, Task, TaskResult, _same_plan, _summarise

LOOK = ("title", "url")


@dataclass
class Thought:
    turn: int
    action: str = ""
    target: str = ""
    reason: str = ""
    source: str = ""
    outcome: str = ""
    ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"turn": self.turn, "action": self.action, "target": truncate(self.target, 80),
                "reason": truncate(self.reason, 160), "source": self.source,
                "outcome": truncate(self.outcome, 160), "ms": self.ms}


class ReasoningEngine(Engine):
    name = "react"
    depends_on = ("actions", "decision", "page", "safety", "memory", "intel")

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.cfg.react
        self.turns_run = 0
        self.stuck = 0

    # -- the loop ------------------------------------------------------
    def run(self, goal: str, seed_plan: Optional[Task] = None,
            approved: bool = False) -> TaskResult:
        self.turns_run += 1
        cfg = self.cfg
        task = seed_plan or Task(goal=goal)
        task.approved = approved
        task.started_at = now()
        task.status = "running"
        result = TaskResult(task_id=task.id, goal=goal, status="running",
                            steps=task.steps, results=task.results)
        thoughts: List[Thought] = []
        for step in task.steps:
            step.status = "pending"

        # journal a redacted *copy*: the live steps keep their real values, or a
        # login would type "<redacted>" into the password field
        safe_steps = [self.engine("safety").redact_step(st, approved=bool(task.approved))
                      for st in task.steps]
        self.ctx.journal.append("task.started", task.id, -1, goal=goal, profile=task.profile,
                                steps=[s.to_dict() for s in safe_steps], mode="react")
        self.ctx.bus.publish("task.started", source=self.name, task=task.id,
                             goal=truncate(goal, 120), steps=len(task.steps), mode="react")
        ui = self.ctx.registry.get("ui") if "ui" in self.ctx.registry else None
        if ui is not None:
            ui.banner(f"SHADOW · {truncate(goal, 58)}",
                      f"task {task.id} · reasoning loop · profile {task.profile or '-'}")

        plan_cursor = 0
        last_page = ""
        unchanged = 0
        answer: Any = None
        failures = 0
        last_error = ""
        # (action, target) pairs already carried out on this page. A loop that
        # re-clicks the same button is not making progress, and the page
        # signature will not change to reveal it when the result is identical.
        done_here: set = set()

        try:
            for turn in range(1, cfg.max_turns + 1):
                if self.ctx.cancelled.is_set():
                    raise TaskAborted("task was cancelled")
                budget = self.engine("safety").budget(task)
                if budget:
                    raise ShadowError(budget)
                model = self._look()
                signature = self._signature(model)
                if signature == last_page and turn > 1:
                    unchanged += 1
                else:
                    if turn > 1:
                        done_here.clear()   # the page moved: old actions are allowed again
                    unchanged = 0
                last_page = signature
                if unchanged >= cfg.stale_after:
                    self.stuck += 1
                    self._line("warn", f"the page has not changed for {unchanged} actions; stopping")
                    result.status = "done" if answer is not None else "stuck"
                    result.results["answer"] = answer
                    self.ctx.journal.append("react.stuck", task.id, turn - 1,
                                            turns=turn - 1, url=model.url)
                    break

                step, source, why = self._decide(goal, model, thoughts, task, plan_cursor, turn,
                                                 failures=failures, done_here=done_here)
                if step is None or step.action == "finish":
                    if failures and answer is None and not self._answered(goal, None):
                        # the loop gave up after real failures: say so instead of
                        # calling a half-done job finished
                        result.status = "failed"
                        result.error = result.error or last_error or \
                            f"gave up after {failures} failed action(s)"
                    else:
                        result.status = "done"
                        # the terminal step of every plan is the "finish" marker
                        for st in task.steps:
                            if st.action == "finish" and st.status == "pending":
                                st.status = "done"
                                st.detail = "the goal was reached"
                                task.steps_done += 1
                        task.step_index = len(task.steps)
                    break
                index = next((i for i, st in enumerate(task.steps) if st is step), -1)
                thought = Thought(turn=turn, action=step.action, target=step.target,
                                  reason=why, source=source)
                started = time.time()
                self._line("action", f"({turn}) {step.label()}   [{source}] {truncate(why, 60)}")
                self.ctx.journal.append("step.started", task.id, turn - 1, action=step.action,
                                        target=step.target, source=source, reason=why)
                self.ctx.bus.publish("step.started", source=self.name, task=task.id,
                                     step=turn - 1, action=step.action,
                                     target=truncate(step.target, 80))
                try:
                    output = self.engine("actions").perform(step, task=task, index=turn - 1)
                except ApprovalRequired:
                    thought.outcome = "needs a human"
                    thoughts.append(thought)
                    result.status = "needs_approval"
                    result.error = "a step needs a person"
                    task.approvals.append({"action": step.action, "target": step.target})
                    result.approvals = task.approvals
                    self._line("warn", f"waiting for a human: {step.label()}")
                    break
                except (ElementNotFound, AmbiguousTarget) as exc:
                    plan = self.engine("recovery").recover(exc)
                    thought.outcome = f"recovered: {exc.message[:80]}"
                    thoughts.append(thought)
                    self.ctx.journal.append("step.recovered", task.id, turn - 1,
                                            action=step.action,
                                            strategies=[a.strategy for a in plan.attempts])
                    failures += 1
                    last_error = last_error or exc.message
                    step.attempts = getattr(step, "attempts", 0) + 1
                    step.detail = exc.message[:160]
                    # recovery already re-tried this step in place; once it has
                    # failed, the cursor moves on or the loop would repeat it
                    plan_cursor = max(plan_cursor, self._cursor_after(task, step, plan_cursor))
                    last_page = ""
                    continue
                except ShadowError as exc:
                    thought.outcome = f"failed: {exc.message[:80]}"
                    thoughts.append(thought)
                    failures += 1
                    last_error = last_error or exc.message
                    self.ctx.journal.append("step.failed", task.id, turn - 1, action=step.action,
                                            error=exc.message[:200])
                    if not getattr(exc, "retryable", False):
                        raise
                    last_page = ""
                    continue

                thought.ms = int((time.time() - started) * 1000)
                done_here.add((step.action, step.target))
                # A plan step that succeeded is ticked off, so the task result
                # reads the same whether a plan or the loop chose the action.
                step.status = "done"
                step.detail = truncate(_summarise(output), 160)
                if index >= 0:
                    task.step_index = max(task.step_index, index + 1)
                if step.action in ("read", "extract") and output not in (None, {}, ""):
                    answer = output
                thought.outcome = _summarise(output)[:160]
                thoughts.append(thought)
                task.steps_done += 1
                if isinstance(output, str) and step.action in ("read", "extract"):
                    task.results["answer"] = output
                self.ctx.journal.append("step.done", task.id, turn - 1, action=step.action,
                                        ms=thought.ms, output=_summarise(output),
                                        source=source)
                self.ctx.bus.publish("step.done", source=self.name, task=task.id,
                                     step=turn - 1, action=step.action, ms=thought.ms)
                self.engine("memory").checkpoint(task.id, turn - 1,
                                                 {"url": model.url, "mode": "react",
                                                  "steps": [(st.action, st.target[:60])
                                                            for st in task.steps]})
                if step is next((st for st in task.steps[plan_cursor:] if st is step), None):
                    plan_cursor += 1
                if step.action == "finish" and answer is None:
                    answer = self._answer_from_task(task)
                if cfg.stop_when_answered and answer is not None and self._answered(goal, answer):
                    result.status = "done"
                    self._line("ok", "the goal looks answered")
                    break
                if turn == cfg.max_turns:
                    result.status = "done" if answer is not None else "exhausted"
                    self.ctx.journal.append("react.exhausted", task.id, turn - 1, turns=turn)
            else:  # pragma: no cover - range always runs the else only when not broken
                result.status = "exhausted"
        except TaskAborted as exc:
            result.status = "cancelled"
            result.error = exc.message
        except ShadowError as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self.ctx.journal.append("task.failed", task.id, task.steps_done, **exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - the loop must end cleanly
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self.fail(exc)
        finally:
            result.ended_at = now()
            task.status = result.status
            if answer is not None:
                result.results["answer"] = answer
            result.results["thoughts"] = [t.to_dict() for t in thoughts]
            result.results["turns"] = len(thoughts)
            if result.status == "done":
                self.engine("memory").store.clear_checkpoint(task.id)
            self.ctx.journal.append("task.ended", task.id, task.steps_done - 1,
                                    status=result.status, error=result.error,
                                    turns=len(thoughts))
            self.ctx.bus.publish("task.finished" if result.status == "done" else "task.failed",
                                 source=self.name, task=task.id, status=result.status,
                                 error=result.error[:200])
        return result

    # -- one turn ------------------------------------------------------
    def _look(self):
        probe = self.ctx.page()
        return self.engine("page").understand(probe, self.ctx.cdp)

    def _decide(self, goal: str, model, thoughts: List[Thought], task: Task,
                cursor: int, turn: int, failures: int = 0, done_here: Optional[set] = None):
        """Return (step, source, why): the plan first, then the model, then the page.

        The plan leads because it is what the person reviewed with `shadow
        plan`. The model is asked when the plan runs out or after a failure,
        and the page is a last resort that may not repeat an action already
        done on this page.
        """
        done_here = done_here or set()
        remaining = [st for st in task.steps[cursor:] if st.action != "finish"]
        if remaining:
            nxt = remaining[0]
            if (nxt.action, nxt.target) not in done_here:
                return nxt, "plan", "the next step of the reviewed plan"
            long_plan = [st for st in remaining if (st.action, st.target) not in done_here]
            if long_plan:
                return long_plan[0], "plan", "the next step of the reviewed plan"

        # The plan is done. Without a failure, the goal has been carried out.
        if failures == 0:
            return None, "plan", "every step of the plan is done"
        if self.cfg.replan_every <= 0 or turn % self.cfg.replan_every == 0 or failures > 0:
            history = [t.to_dict() for t in thoughts]
            proposal = self.engine("intel").reason(goal, model.to_dict(), history)
            if proposal:
                step = self._step_from_model(proposal, model)
                if step is not None and (step.action, step.target) not in done_here:
                    return step, "model", proposal.get("thought", "the local model proposed it")
        if failures >= self.cfg.stale_after:
            # Three failed actions on the same page is a wall, not a puzzle.
            return None, "plan", f"{failures} actions failed; stopping instead of guessing"
        page_step = self._from_page(goal, model)
        if page_step is not None and (page_step.action, page_step.target) not in done_here:
            return page_step, "page", "the plan is spent; the page suggests this"
        return None, "page", "nothing left that would change the page"

    def _step_from_model(self, proposal: Dict[str, str], model) -> Optional[Step]:
        action = proposal.get("action", "")
        target = proposal.get("target", "")
        value = proposal.get("value", "")
        allowed = self.ctx.cfg.intelligence.allow_actions
        mapping = {"click": "click", "type": "fill", "press": "press", "scroll": "scroll",
                   "wait": "wait", "goto": "goto", "extract": "read", "assert": "assert",
                   "select": "select", "submit": "submit", "remember": "remember",
                   "finish": "finish", "back": "back", "reload": "reload",
                   "branch": "wait", "hover": "hover"}
        if action not in allowed:
            self.emit("rejected", reason=f"action {action!r} is not allowed")
            return None
        mapped = mapping.get(action)
        if mapped is None:
            return None
        if mapped in ("click", "hover", "select") and target:
            known = {e.get("selector") for e in model.elements}
            known |= {e.get("name") for e in model.elements}
            if target not in known and not any(target.lower() in (k or "").lower() for k in known):
                self.emit("rejected", reason="target is not on the page", target=target[:60])
                return None
        return Step(mapped, target=target, value=value)

    def _from_page(self, goal: str, model) -> Optional[Step]:
        """Last resort, and it is honest about being a guess."""
        lowered = goal.lower()
        for field in model.fields:
            name = (field.get("name") or "").lower()
            if name and name in lowered:
                return Step("fill", target=field.get("selector") or name, value="")
        for button in model.buttons:
            name = (button.get("name") or "").lower()
            if name and name in lowered:
                return Step("click", target=button.get("selector") or name)
        if model.links and "open" in lowered:
            return Step("click", target=model.links[0].get("selector") or model.links[0].get("name", ""))
        return None

    # -- helpers -------------------------------------------------------
    def _signature(self, model) -> str:
        return "|".join([model.url, model.title,
                         ",".join(str(f.get("name", "")) for f in model.fields[:8]),
                         ",".join(str(b.get("name", "")) for b in model.buttons[:8]),
                         (model.text or "")[:120]])

    def _answered(self, goal: str, answer: Any) -> bool:
        if not self.cfg.stop_when_answered or answer in (None, "", {}, []):
            return False
        text = answer if isinstance(answer, str) else str(answer)
        if len(text.strip()) < 2:
            return False
        lowered = goal.lower()
        return any(word in lowered for word in ("read", "what", "price", "how many", "اقرأ", "ما هو",
                                                "كم", "lis", "quelle", "combien"))

    @staticmethod
    def _cursor_after(task: Task, step: Step, cursor: int) -> int:
        for i, candidate in enumerate(task.steps[cursor:], start=cursor):
            if candidate is step:
                return i + 1
        return cursor + 1

    def _line(self, level: str, text: str) -> None:
        ui = self.ctx.registry.get("ui") if "ui" in self.ctx.registry else None
        if ui is not None:
            ui.notify(level, text)
        else:
            self.log.info("%s", text)

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"turns_run": self.turns_run, "stuck": self.stuck,
                     "max_turns": self.cfg.max_turns, "enabled": self.cfg.enabled})
        return info

    def _answer_from_task(self, task) -> Any:
        """Whatever the successful steps already produced, as the task answer."""
        got = getattr(task, "results", {}) or {}
        return got.get("answer")
