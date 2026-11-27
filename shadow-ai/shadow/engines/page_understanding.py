"""Page Understanding Engine — what is this page, and what matters on it?

It reads structure, not pixels: headings, forms, links, buttons, tables and
alerts, plus a plain-text projection. The output (`PageModel`) is what the
planner reasons over and what the model bridge is shown.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .base import Engine

ALERT_SELECTORS = (".error,.alert,.warning,[role=alert],[aria-live=assertive],.invalid-feedback,"
                   ".notice,.toast,.banner")
KIND_HINTS = {
    "login": ["login", "sign in", "signin", "password", "connexion", "تسجيل الدخول", "كلمة المرور"],
    "search": ["search", "results", "recherche", "بحث"],
    "checkout": ["checkout", "cart", "payment", "panier", "paiement", "الدفع", "السلة"],
    "list": ["product", "results", "list", "table", "items"],
    "detail": ["price", "add to cart", "details", "description"],
    "error": ["error", "not found", "404", "500", "refusé", "خطأ"],
}


@dataclass
class PageModel:
    url: str = ""
    title: str = ""
    kind: str = "unknown"
    headings: List[str] = field(default_factory=list)
    text: str = ""
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)
    fields: List[Dict[str, Any]] = field(default_factory=list)
    forms: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[List[str]] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    scroll: float = 0.0
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["elements"] = self.elements   # the flat list a planner iterates
        return data

    @property
    def elements(self) -> List[Dict[str, Any]]:
        out = list(self.buttons)
        seen = {e.get("selector") for e in out}
        for item in self.fields + self.links:
            if item.get("selector") not in seen:
                out.append(item)
                seen.add(item.get("selector"))
        return out

    def brief(self, limit: int = 900) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", f"KIND: {self.kind}"]
        if self.headings:
            lines.append("HEADINGS: " + " | ".join(self.headings[:6]))
        if self.fields:
            lines.append("FIELDS: " + ", ".join(f.get("name", "?") for f in self.fields[:10]))
        if self.buttons:
            lines.append("BUTTONS: " + ", ".join(b.get("name", "?") for b in self.buttons[:10]))
        if self.alerts:
            lines.append("ALERTS: " + " | ".join(self.alerts[:3]))
        lines.append("TEXT: " + self.text[:limit])
        return "\n".join(lines)


class PageUnderstandingEngine(Engine):
    name = "page"

    def understand(self, probe, cdp=None) -> PageModel:
        model = PageModel()
        try:
            model.url = (cdp.current_url() if cdp else "") or ""
            model.title = (cdp.title() if cdp else "") or ""
        except Exception:
            pass
        parts = urlsplit(model.url)
        model.path = parts.path or "/"

        try:
            nodes = probe.snapshot(limit=700)
        except Exception as exc:  # noqa: BLE001 - a page that cannot be read is still a page
            self.fail(exc)
            nodes = []

        for node in nodes:
            if not node.get("visible"):
                continue
            tag = node.get("tag", "")
            role = node.get("role", "")
            if tag in ("h1", "h2", "h3") and node.get("text"):
                model.headings.append(node["text"][:120])
            elif tag == "button" or role == "button" or node.get("type") in ("submit", "button"):
                model.buttons.append(_slim(node))
            elif tag == "a" and node.get("href"):
                model.links.append(_slim(node, href=True))
            elif tag in ("input", "textarea", "select") and node.get("type") != "hidden":
                model.fields.append(_slim(node, extra={"type": node.get("type", tag)}))
            elif tag in ("td", "th", "li") and node.get("text"):
                model.text += (" " + node["text"]) if model.text else node["text"]

        try:
            model.forms = probe.forms() or []
        except Exception:
            pass
        try:
            model.tables = self._tables(probe)
        except Exception:
            pass
        try:
            model.alerts = [a for a in probe.texts(ALERT_SELECTORS) if a][:5]
        except Exception:
            pass
        try:
            body = probe.ask("html", None)
            model.text = re.sub(r"\s+", " ", body or model.text)[:6000]
        except Exception:
            model.text = re.sub(r"\s+", " ", model.text)[:6000]
        if cdp is not None:
            try:
                model.scroll = float(probe.ask("scrollY", None) or 0)
            except Exception:
                model.scroll = 0.0

        model.kind = self.classify(model)
        self.note(url=model.url, kind=model.kind)
        self.emit("understood", url=model.url, kind=model.kind,
                  fields=len(model.fields), buttons=len(model.buttons), alerts=len(model.alerts))
        return model

    # -- classification ------------------------------------------------
    @staticmethod
    def classify(model: PageModel) -> str:
        hay = f"{model.title} {model.path} {model.text[:1500]}".lower()
        scores = {kind: sum(1 for hint in hints if hint in hay) for kind, hints in KIND_HINTS.items()}
        has_password = any((f.get("type") == "password") for f in model.fields)
        if has_password:
            scores["login"] += 3
        if any("search" in (f.get("name", "") + f.get("type", "")).lower() for f in model.fields):
            scores["search"] += 2
        if len(model.links) >= 8 and len(model.fields) <= 2:
            scores["list"] += 2
        if not model.text and not model.headings:
            return "blank"
        best = max(scores.items(), key=lambda kv: kv[1])
        return best[0] if best[1] > 0 else "generic"

    @staticmethod
    def _tables(probe) -> List[List[str]]:
        rows = probe.snapshot("table tr", limit=40)
        by_row: Dict[str, List[str]] = {}
        for row in rows:
            by_row.setdefault(row.get("selector", "").rsplit(" > ", 1)[0], []).append(row.get("text", ""))
        return [cells for cells in by_row.values() if cells][:5]

    # -- question answering over the page itself -----------------------
    def relevant(self, model: PageModel, question: str, limit: int = 6) -> List[str]:
        """Pick the parts of the page that answer a question, without a model.

        Terms are located in the page text and a window is cut around each
        hit, so an answer is the neighbourhood of the word, not the whole
        page. Windows that hold more distinct terms win. When no term is on
        the page, the opening of the text is returned instead of nothing —
        "not found on this page" is a fact the caller is allowed to see.
        """
        terms = [w for w in re.findall(r"[\w\u0600-\u06ff]+", (question or "").lower()) if len(w) > 2]
        text = re.sub(r"\s+", " ", model.text or "").strip()
        if not text:
            return model.alerts[:limit]
        if not terms:
            return _windows(text, 180, 180)[:limit]

        low = text.lower()
        windows: List[tuple] = []
        for term in terms:
            start = 0
            hits = 0
            while hits < 6:
                at = low.find(term, start)
                if at < 0:
                    break
                hits += 1
                left = max(0, at - 80)
                right = min(len(text), at + len(term) + 120)
                snippet = text[left:right]
                snippet = re.sub(r"^\S*\s", "", snippet) if left > 0 else snippet
                snippet = re.sub(r"\s\S*$", "", snippet) if right < len(text) else snippet
                windows.append((snippet, left))
                start = at + len(term)

        if not windows:
            return _windows(text, 200, 200)[:limit]

        scored = []
        seen = set()
        for snippet, left in windows:
            key = (left // 60, snippet[:40])
            if key in seen:
                continue
            seen.add(key)
            snippet_low = snippet.lower()
            distinct = sum(1 for t in set(terms) if t in snippet_low)
            scored.append((distinct, -left, snippet.strip()))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        out: List[str] = []
        for _, _, snippet in scored:
            if snippet and snippet not in out:
                out.append(snippet)
            if len(out) >= limit:
                break
        return out

    def answer(self, model: PageModel, question: str) -> Dict[str, Any]:
        lines = self.relevant(model, question)
        return {"question": question, "answer": " ".join(lines)[:800] if lines else "",
                "sources": lines[:5], "kind": model.kind, "url": model.url}


def _windows(text: str, size: int, stride: int) -> List[str]:
    """Overlapping windows over a page with no usable sentence breaks."""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= size:
        return [text]
    return [text[i:i + size] for i in range(0, len(text) - stride, stride)]


def _slim(node: Dict[str, Any], href: bool = False, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {"name": (node.get("name") or node.get("text") or "")[:80],
           "tag": node.get("tag"), "selector": node.get("selector"),
           "role": node.get("role"), "rect": node.get("rect")}
    if href:
        out["href"] = node.get("href", "")
    if extra:
        out.update(extra)
    return out
