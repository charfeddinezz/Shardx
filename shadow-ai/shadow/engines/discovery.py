"""Discovery Engine — find the thing the goal means, on a page nobody wrote for us.

Every candidate is scored, never guessed. When the best two candidates are
close, `pick()` refuses rather than clicking the wrong button; the planner
then re-reads the page or asks for a narrower label. That refusal is the point
of this engine.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import AmbiguousTarget, ElementNotFound
from .base import Engine

STOPWORDS = {
    "the", "a", "an", "on", "in", "at", "of", "to", "for", "with", "and", "or", "my", "me",
    "le", "la", "les", "un", "une", "des", "du", "sur", "dans", "de", "au", "aux", "et",
    "على", "في", "من", "إلى", "الى", "عن", "ثم", "مع", "ال",
}
ROLE_WORDS = {
    "button": "button", "btn": "button", "زر": "button", "bouton": "button",
    "link": "link", "انقر": "link", "رابط": "link", "lien": "link",
    "field": "field", "input": "field", "box": "field", "حقل": "field", "champ": "field",
    "checkbox": "checkbox", "radio": "radio", "select": "select", "dropdown": "select",
    "tab": "tab", "menu": "menu", "search": "search", "searchbox": "search",
    "header": "heading", "heading": "heading", "title": "heading", "عنوان": "heading",
}
DEFAULT_SELECTOR = ("a,button,input,select,textarea,label,option,summary,[role],[onclick],"
                    "h1,h2,h3,h4,li,td,[class*=button],[class*=btn]")


@dataclass
class Candidate:
    node: Dict[str, Any]
    score: float
    why: List[str] = field(default_factory=list)

    @property
    def selector(self) -> str:
        return self.node.get("selector") or ""

    @property
    def label(self) -> str:
        return self.node.get("name") or self.node.get("text") or self.node.get("tag", "")

    def to_dict(self) -> Dict[str, Any]:
        return {"score": round(self.score, 1), "label": self.label[:80], "tag": self.node.get("tag"),
                "selector": self.selector, "why": self.why}


class DiscoveryEngine(Engine):
    name = "discovery"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.last_results: List[Candidate] = []
        self.ambiguous = 0
        self.matched_lesson = ""     # last time a remembered selector was used
        self.forgot_lesson = ""      # last time one had gone stale

    # -- text helpers -------------------------------------------------
    @staticmethod
    def keywords(description: str) -> Tuple[List[str], str]:
        text = (description or "").lower()
        role = ""
        for word, mapped in ROLE_WORDS.items():
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
                role = mapped
                text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", " ", text)
                break
        words = [w for w in re.findall(r"[\w\u0600-\u06ff-]+", text) if w not in STOPWORDS and len(w) > 1]
        return words, role

    @staticmethod
    def similarity(a: str, b: str) -> float:
        a, b = (a or "").lower().strip(), (b or "").lower().strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.85
        return difflib.SequenceMatcher(None, a, b).ratio()

    # -- search -------------------------------------------------------
    def search(self, probe, description: str, selector: str = "", limit: int = 8,
               interactive_only: bool = False) -> List[Candidate]:
        words, role = self.keywords(description)
        nodes = probe.snapshot(selector or DEFAULT_SELECTOR, limit=600)
        results: List[Candidate] = []
        for node in nodes:
            if not node.get("visible"):
                continue
            if interactive_only and not node.get("interactive"):
                continue
            score, why = self._score(node, words, role, description)
            if score > 0:
                results.append(Candidate(node=node, score=score, why=why))
        results.sort(key=lambda c: (-c.score, not c.node.get("interactive"),
                                    c.node.get("rect", {}).get("y", 0)))
        self.last_results = results[:limit]
        self.note(description=description[:60], matches=len(results))
        self.emit("found", description=description[:60],
                  top=[c.to_dict() for c in results[:3]])
        return self.last_results

    def _score(self, node: Dict[str, Any], words: List[str], role: str,
               description: str) -> Tuple[float, List[str]]:
        name = (node.get("name") or "").lower()
        text = (node.get("text") or "").lower()
        ident = (node.get("id") or "").lower()
        attrs = " ".join(f"{k} {v}" for k, v in (node.get("attrs") or {}).items() if k not in ("type", "value")).lower()
        classes = " ".join(node.get("classes") or []).lower()
        haystack = " ".join([name, text, ident, attrs, classes])
        node_role = (node.get("role") or node.get("tag") or "").lower()
        score = 0.0
        why: List[str] = []

        phrase = (description or "").lower().strip()
        sim_name = self.similarity(phrase, name)
        sim_text = self.similarity(phrase, text)
        if phrase and phrase == name:
            score += 120
            why.append("exact name")
        elif sim_name >= 0.9:
            score += 80 * sim_name
            why.append("name close")
        elif sim_name >= 0.6:
            score += 45 * sim_name
            why.append("name partial")
        if phrase and phrase == text:
            score += 100
            why.append("exact text")
        elif sim_text >= 0.85:
            score += 60 * sim_text
            why.append("text close")
        elif phrase and text and phrase in text:
            score += 35
            why.append("text contains")

        hits = [w for w in words if w in haystack]
        coverage = (len(hits) / len(words)) if words else 0.0
        score += 55 * coverage
        if hits:
            why.append("words " + ",".join(hits[:4]))

        # Being "a button" is not evidence that it is *the* button. Without a
        # word, a name, or a text that resembles the description, the role is
        # not allowed to carry the match over the threshold.
        textual = coverage > 0 or sim_name >= 0.6 or sim_text >= 0.85
        if not textual:
            score *= 0.15
            if words:
                why.append("no textual evidence")

        if role:
            if role in ("button", "link") and node_role == role:
                score += 14 if textual else 0
                why.append(f"is {role}")
            elif role == "field" and node.get("tag") in ("input", "textarea", "select"):
                score += 14
                why.append("is field")
            elif role in (node_role, node.get("type", "")):
                score += 12
                why.append(f"is {role}")
            elif role == "search" and (node.get("type") == "search" or "search" in haystack):
                score += 12
                why.append("is search")
            else:
                score -= 10

        if node.get("interactive"):
            score += 12
        if node.get("disabled"):
            score -= 60
            why.append("disabled")
        if node.get("tag") == "option":
            score -= 15

        return score, why

    # -- picking ------------------------------------------------------
    def pick(self, probe, description: str, selector: str = "", threshold: float = 35.0,
             margin: float = 12.0) -> Candidate:
        # A selector this site taught us last time outranks any score: it is
        # evidence from a real run, not a guess from this one. It is dropped the
        # moment it no longer points at something on the page.
        if not selector and "memory" in self.ctx.registry:
            memory = self.ctx.registry.get("memory")
            remembered = None
            for lesson in memory.store.data.get("lessons", {}).values():
                # lessons are small; checking all of them means a goal typed
                # without the URL still finds the one this site taught us
                entry = lesson.get("click:" + description) if isinstance(lesson, dict) else None
                if isinstance(entry, dict) and entry.get("detail"):
                    remembered = entry["detail"]
                    break
                if isinstance(entry, str):
                    remembered = entry
                    break
            if isinstance(remembered, str) and remembered:
                hits = probe.snapshot(remembered, limit=2)
                if hits and hits[0].get("visible"):
                    self.matched_lesson = remembered
                    return Candidate(node=hits[0], score=100.0,
                                     why=[f"remembered selector {remembered}"])
                else:
                    self.forgot_lesson = remembered
                    self.emit("lesson_stale", selector=remembered[:60], target=description[:40])
        results = self.search(probe, description, selector=selector)
        usable = [c for c in results if c.score >= threshold and not c.node.get("disabled")]
        if not usable:
            raise ElementNotFound(f"nothing on the page matches {description!r}",
                                  suggestions=[c.to_dict() for c in results[:3]])
        best = usable[0]
        if len(usable) > 1 and usable[1].score >= best.score - margin:
            self.ambiguous += 1
            self.emit("ambiguous", description=description[:60],
                      best=best.to_dict(), second=usable[1].to_dict())
            raise AmbiguousTarget(f"{description!r} matches {usable[0].label!r} and "
                                  f"{usable[1].label!r} equally well",
                                  options=[c.to_dict() for c in usable[:4]])
        return best

    def pick_or_none(self, probe, description: str, **kw) -> Optional[Candidate]:
        try:
            return self.pick(probe, description, **kw)
        except (ElementNotFound, AmbiguousTarget):
            return None

    # -- specialised lookups ------------------------------------------
    def find_field(self, probe, label: str) -> Optional[Candidate]:
        """A text field, by label / placeholder / name / type hint."""
        # "the search field" says "field" is grammar, not a hint: keep the rest.
        label = re.sub(r"(?i)\b(field|input|box|حقل|champ|textbox)\b", " ", label or "")
        words = [w for w in re.findall(r"[\w\u0600-\u06ff-]+", label.lower())
                 if w not in STOPWORDS and len(w) > 1]
        words += self.kinds_of(label)
        candidates = [n for n in probe.snapshot("input,textarea,select,[contenteditable]", limit=200)
                      if n.get("visible") and not n.get("disabled")]
        if not candidates:
            return None
        best, best_score = None, 0.0
        for node in candidates:
            hay = " ".join([node.get("name", ""), node.get("id", ""),
                            (node.get("attrs") or {}).get("placeholder", ""),
                            (node.get("attrs") or {}).get("name", ""),
                            (node.get("attrs") or {}).get("autocomplete", ""),
                            node.get("label", ""), node.get("text", ""),
                            node.get("role", ""), node.get("type", "")]).lower()
            score = 0.0
            for word in words:
                if word == node.get("type", ""):
                    score += 70
                if word in hay:
                    score += 45
                if word == (node.get("attrs") or {}).get("name", "").lower():
                    score += 30
                score += 30 * self.similarity(word, (node.get("attrs") or {}).get("placeholder", ""))
            if node.get("tag") == "select" and ("select" in words or not node.get("type")):
                score += 20      # a select is only ever one thing
            if not words:
                score = 20
            if score > best_score:
                best, best_score = node, score
        if best is None or best_score < 25:
            return None
        return Candidate(node=best, score=best_score, why=["field match"])

    @staticmethod
    def kinds_of(label: str) -> List[str]:
        """"champ de recherche" and "حقل البريد" mean search and email.

        The lexicon is the intelligence engine's FIELD_HINTS, used as data:
        it is a plain dict, so this is not an engine dependency. Each matched
        kind contributes its English word, which is what a field's type,
        name or placeholder is written in.
        """
        from .intelligence import FIELD_HINTS
        low = (label or "").lower()
        return [kind for kind, hints in FIELD_HINTS.items()
                if any(re.search(rf"(?<!\w){re.escape(h)}(?!\w)", low) for h in hints)]

    def find_by_selector(self, probe, selector: str) -> Optional[Dict[str, Any]]:
        nodes = probe.snapshot(selector, limit=3)
        return nodes[0] if nodes else None

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info["ambiguous_so_far"] = self.ambiguous
        info["lesson_hits"] = self.matched_lesson[:80]
        info["lesson_stale"] = self.forgot_lesson[:80]
        return info
