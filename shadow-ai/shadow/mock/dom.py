"""The mock's DOM: building, querying, and the SHADOW probe protocol."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .html import Node, parse

TEXT_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "span", "a", "button", "td", "th",
             "label", "option", "div", "title", "strong", "em", "small", "legend"}
INTERACTIVE = {"a", "button", "input", "select", "textarea", "option", "summary"}
SIMPLE_SELECTOR = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*|\*)?"
    r"(?P<id>#[\w-]+)?"
    r"(?P<classes>(?:\.[\w-]+)*)"
    r"(?P<attrs>(?:\[[^\]]+\])*)$"
)


class MockDocument:
    def __init__(self, url: str):
        self.url = url
        self.nodes: List[Node] = []
        self.root: Optional[Node] = None
        self.title = ""
        self.focus_id = ""
        self.values: Dict[str, str] = {}
        self.scroll_y = 0
        self.timers: List[Dict[str, Any]] = []
        self.mutations = 0
        self.load(url, "")

    # -- loading ------------------------------------------------------
    def load(self, url: str, html: str) -> None:
        builder = parse(html)
        self.url = url
        self.root = builder.root
        self.nodes = [n for n in self.root.walk() if n.tag not in ("#document", "script", "head", "html", "body")]
        titles = [n for n in self.root.walk() if n.tag == "title"]
        self.title = titles[0].inner_text() if titles else ""
        self.values = {}
        self.focus_id = ""
        for node in self.nodes:
            if node.tag in ("input", "textarea", "select"):
                self.values[_key(node)] = node.get("value", "")
            if node.get("autofocus"):
                self.focus_id = _key(node)
        self.mutations += 1
        for script in _script_bodies(self.root):
            self._schedule_script(script)

    def _schedule_script(self, script: str) -> None:
        for body, delay in re.findall(r"setTimeout\s*\(\s*function\s*\(\)\s*\{(.*?)\}\s*,\s*(\d+)\s*\)", script, re.S):
            self.timers.append({"due": time.time() + int(delay) / 1000.0, "body": body})

    def tick(self) -> None:
        """Run any timers that are due. Called before every read."""
        now = time.time()
        due = [t for t in self.timers if t["due"] <= now]
        self.timers = [t for t in self.timers if t["due"] > now]
        for timer in due:
            self._run_fragment(timer["body"])

    def _run_fragment(self, body: str) -> None:
        pattern = r"document\.getElementById\('([^']+)'\)\.innerHTML\s*=\s*(['\"])(.*?)\2"
        for match in re.finditer(pattern, body, re.S):
            node = self.by_id(match.group(1))
            if node is None:
                continue
            frag = parse(match.group(3)).root
            node.children = list(frag.children)
            for child in node.children:
                child.parent = node
            self._reindex()
            self.mutations += 1

    def append_html(self, html: str) -> None:
        """Append parsed nodes to <body>, as insertAdjacentHTML(beforeend) does."""
        body = self.root_children_first("body")
        if body is None:
            return
        frag = parse(html).root
        for child in list(frag.children):
            child.parent = body
            body.children.append(child)
        self._reindex()
        self.mutations += 1

    def root_children_first(self, tag: str):
        if self.root is None:
            return None
        for node in self.root.walk():
            if node.tag == tag:
                return node
        return None

    def _reindex(self) -> None:
        if self.root is None:
            return
        self.nodes = [n for n in self.root.walk()
                      if n.tag not in ("#document", "script", "head", "html", "body")]
        for node in self.nodes:
            if node.tag in ("input", "textarea", "select"):
                self.values.setdefault(_key(node), node.get("value", ""))

    # -- lookup -------------------------------------------------------
    def by_id(self, ident: str) -> Optional[Node]:
        for node in self.nodes:
            if node.id == ident:
                return node
        return None

    def query(self, selector: str) -> List[Node]:
        selector = (selector or "").strip()
        if not selector:
            return []
        out: List[Node] = []
        for group in [g.strip() for g in selector.split(",") if g.strip()]:
            parts = group.split()
            candidates = self._match_compound(parts[-1], self.nodes)
            for depth in range(len(parts) - 2, -1, -1):
                keep = []
                for node in candidates:
                    if any(_match_one(anc, parts[depth]) for anc in node.ancestors()):
                        keep.append(node)
                candidates = keep
            out.extend(candidates)
        seen, uniq = set(), []
        for node in out:
            if id(node) not in seen:
                seen.add(id(node))
                uniq.append(node)
        return uniq

    def _match_compound(self, compound: str, pool: List[Node]) -> List[Node]:
        return [n for n in pool if _match_one(n, compound)]

    # -- probe protocol ----------------------------------------------
    def probe(self, name: str, arg: Any) -> Any:
        self.tick()
        if name == "snapshot":
            selector = (arg or {}).get("selector") or ""
            limit = int((arg or {}).get("limit") or 200)
            nodes = self.query(selector) if selector else self.nodes
            return [self.describe(node) for node in nodes[:limit]]
        if name == "text":
            nodes = self.query(str(arg))
            return nodes[0].inner_text() if nodes else None
        if name == "texts":
            return [n.inner_text() for n in self.query(str(arg))]
        if name == "attribute":
            selector, attr = arg
            nodes = self.query(selector)
            return nodes[0].get(attr) if nodes else None
        if name == "count":
            return len(self.query(str(arg)))
        if name == "active":
            node = self.by_id(self.focus_id) if self.focus_id else None
            return self.describe(node, active=True) if node else None
        if name == "value":
            node = self.by_id(str(arg))
            return self.values.get(str(arg), node.get("value") if node else None)
        if name == "setValue":
            key, value = arg
            self.values[key] = value
            self.mutations += 1
            return True
        if name == "html":
            return self.root.inner_text()
        if name == "links":
            return [{"text": n.inner_text(), "href": n.get("href")} for n in self.nodes if n.tag == "a"]
        if name == "scrollY":
            return self.scroll_y
        if name == "ready":
            return "complete"
        if name == "forms":
            out = []
            for node in self.nodes:
                if node.tag != "form":
                    continue
                fields = [{"name": f.get("name"), "type": (f.get("type") or f.tag),
                           "required": f.get("required") != "", "label": _accessible_name(f)}
                          for f in node.walk() if f.tag in ("input", "textarea", "select")]
                out.append({"action": self.url, "method": node.get("method", "get"), "fields": fields})
            return out
        return {"error": f"unknown probe {name}"}

    def describe(self, node: Node, active: bool = False) -> Dict[str, Any]:
        rect = self.rect(node)
        key = _key(node)
        info = {
            "tag": node.tag,
            "id": node.id,
            "classes": node.classes,
            "text": node.inner_text()[:300],
            "attrs": {k: v for k, v in node.attrs.items() if k != "class"},
            "rect": rect,
            "visible": rect["width"] > 0 and rect["height"] > 0,
            "interactive": node.tag in INTERACTIVE or node.get("onclick") != "",
            "role": node.get("role") or _role(node),
            "name": _accessible_name(node),
            "value": self.values.get(key, node.get("value", "")),
            "checked": node.get("checked", "") != "",
            "disabled": ("disabled" in node.attrs) or node.get("aria-disabled") == "true",
            "href": node.get("href", ""),
            "type": node.get("type", ""),
            "active": active or key == self.focus_id,
            "key": key,
            "selector": self.css_path(node),
        }
        return info

    def css_path(self, node: Node) -> str:
        if node.id:
            return f"#{node.id}"
        bits = [node.tag]
        if node.classes:
            bits.append("." + ".".join(node.classes))
        return "".join(bits)

    def rect(self, node: Node) -> Dict[str, float]:
        """Deterministic layout: block flow, 24px tall rows, 12px gaps."""
        top = 0.0
        width = 1000.0
        for other in self.nodes:
            if other is node:
                break
            if other.tag in ("html", "body", "head"):
                continue
            top += self._height(other) + 10
        top -= self.scroll_y
        height = self._height(node)
        if node.tag in ("html", "body", "head"):
            return {"x": 0.0, "y": 0.0, "width": 1000.0, "height": 700.0}
        if node.parent is not None and node.parent.tag in ("td", "th"):
            return {"x": 40.0, "y": max(0.0, top), "width": 160.0, "height": height}
        if node.tag == "input":
            width = 280.0
        elif node.tag in ("button", "select"):
            width = 150.0
        elif node.tag == "textarea":
            width = 420.0
            height = 90.0
        return {"x": 90.0, "y": max(0.0, top), "width": width, "height": height}

    @staticmethod
    def _height(node: Node) -> float:
        if node.tag == "textarea":
            return 90.0
        if node.tag in ("h1", "h2"):
            return 44.0
        if node.tag in ("table", "tr"):
            return 34.0
        if node.tag in ("input", "button", "select"):
            return 34.0
        return 26.0

    def center(self, info: Dict[str, Any]) -> Dict[str, float]:
        rect = info.get("rect") or {}
        return {"x": rect.get("x", 0) + rect.get("width", 0) / 2,
                "y": rect.get("y", 0) + rect.get("height", 0) / 2}


def _key(node: Node) -> str:
    return node.id or f"@{node.tag}:{id(node) % 100000}"


def _role(node: Node) -> str:
    if node.tag == "a":
        return "link"
    if node.tag == "button":
        return "button"
    if node.tag == "input":
        return {"search": "searchbox", "email": "textbox", "password": "textbox"}.get(node.get("type"), "textbox")
    if node.tag == "select":
        return "combobox"
    if node.tag == "textarea":
        return "textbox"
    if node.tag in ("h1", "h2", "h3"):
        return "heading"
    return ""


def _accessible_name(node: Node) -> str:
    for attr in ("aria-label", "placeholder", "name", "title", "alt"):
        if node.get(attr):
            return node.get(attr)
    if node.tag in INTERACTIVE or node.tag in TEXT_TAGS:
        return node.inner_text()[:120]
    return ""


def _match_one(node: Node, compound: str) -> bool:
    match = SIMPLE_SELECTOR.match(compound)
    if not match:
        return False
    tag, ident, classes, attrs = match.group("tag"), match.group("id"), match.group("classes"), match.group("attrs")
    if tag and tag != "*" and node.tag != tag.lower():
        return False
    if ident and node.id != ident[1:]:
        return False
    if classes:
        for cls in classes.split(".")[1:]:
            if cls and cls not in node.classes:
                return False
    if attrs:
        for raw in re.findall(r"\[([^\]]+)\]", attrs):
            if "=" in raw:
                name, value = raw.split("=", 1)
                if node.get(name.strip()) != value.strip().strip("'\""):
                    return False
            elif not node.get(raw.strip()):
                return False
    return True


def _script_bodies(root: Node) -> List[str]:
    return [n.text for n in root.walk() if n.tag == "script"]
