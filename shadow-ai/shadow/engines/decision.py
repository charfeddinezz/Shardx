"""Decision Engine — pick the next action, and refuse to be talked into a bad one.

Three sources, in order of preference:

1. the reviewed plan (deterministic, what `plan` printed);
2. the local model, when one is configured — its proposal is validated
   against the same action set the planner uses, and a proposal that names a
   target not on the page is discarded;
3. a page-driven fallback: if the page has a search field the goal mentions,
   use it; if it has one obvious submit, press it.

Whatever the source, the Safety Engine checks the result before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import Engine

VALID_ACTIONS = {"goto", "click", "type", "press", "scroll", "wait", "extract", "assert",
                 "back", "reload", "hover", "select", "submit", "remember", "finish"}


@dataclass
class Decision:
    action: str
    target: str = ""
    value: str = ""
    source: str = "plan"
    thought: str = ""
    confidence: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "target": self.target[:120], "value": self.value[:120],
                "source": self.source, "thought": self.thought[:200],
                "confidence": round(self.confidence, 2)}


class DecisionEngine(Engine):
    name = "decision"
    depends_on = ("intel",)

    def __init__(self, ctx):
        super().__init__(ctx)
        self.decisions: List[Decision] = []
        self.rejected = 0

    def choose(self, goal: str, model_page: Dict[str, Any], history: List[Dict[str, Any]],
               allow_model: bool = True) -> Decision:
        """One step, from the best available source."""
        if allow_model:
            proposal = self.engine("intel").reason(goal, model_page, history)
            if proposal:
                decision = self._validate(proposal, model_page)
                if decision is not None:
                    self.decisions.append(decision)
                    self.note(source="model")
                    self.emit("chose", **decision.to_dict())
                    return decision
        decision = self._from_page(goal, model_page)
        self.decisions.append(decision)
        self.note(source=decision.source)
        self.emit("chose", **decision.to_dict())
        return decision

    def _validate(self, proposal: Dict[str, str], model_page: Dict[str, Any]) -> Optional[Decision]:
        action = (proposal.get("action") or "").strip()
        target = (proposal.get("target") or "").strip()
        if action not in VALID_ACTIONS or action not in self.ctx.cfg.intelligence.allow_actions:
            self.rejected += 1
            self.emit("rejected", reason=f"action {action!r} is not allowed", target=target[:60])
            return None
        if action in ("click", "select") and target:
            known = {e.get("selector") for e in model_page.get("elements", [])}
            known |= {e.get("name") for e in model_page.get("elements", [])}
            if target not in known and not any(target.lower() in (k or "").lower() for k in known):
                self.rejected += 1
                self.emit("rejected", reason="target is not on the page", target=target[:60])
                return None
        return Decision(action=action, target=target, value=proposal.get("value", ""),
                        source="model", thought=proposal.get("thought", ""), confidence=0.7)

    def _from_page(self, goal: str, model_page: Dict[str, Any]) -> Decision:
        words = {w.lower() for w in (goal or "").split() if len(w) > 3}
        for element in model_page.get("elements") or []:
            label = f"{element.get('name', '')} {element.get('text', '')}".lower()
            if not words or any(w in label for w in words):
                action = "type" if element.get("tag") in ("input", "textarea") else "click"
                return Decision(action=action, target=element.get("selector", ""),
                                value=goal if action == "type" else "", source="page",
                                thought="the goal names something on the page", confidence=0.4)
        if model_page.get("title"):
            return Decision(action="extract", target="page", source="page",
                            thought="no obvious action; read the page", confidence=0.2)
        return Decision(action="finish", source="page", thought="nothing to do", confidence=0.1)

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"decisions": len(self.decisions), "rejected": self.rejected})
        return info
