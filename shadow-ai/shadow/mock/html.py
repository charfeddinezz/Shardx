"""A forgiving HTML parser for the mock engine — enough for `pages.py`."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag: str, attrs: dict = None, parent: "Node" = None):
        self.tag = tag.lower()
        self.attrs = dict(attrs or {})
        self.children: List["Node"] = []
        self.text = ""
        self.parent = parent

    # -- traversal ----------------------------------------------------
    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def inner_text(self) -> str:
        parts = [self.text]
        for child in self.children:
            parts.append(child.inner_text())
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def all_text(self) -> str:
        return self.inner_text()

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def classes(self) -> List[str]:
        return [c for c in self.attrs.get("class", "").split() if c]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag}{'#' + self.id if self.id else ''}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]
        self.scripts: List[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag in VOID:
            return
        self.stack.append(node)
        if tag == "script":
            self._in_script = True

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._in_script:
            self.stack[-1].text += data
            return
        if data.strip():
            anchor = self.stack[-1]
            if not anchor.children and anchor.text:
                anchor.text += " " + data
            else:
                anchor.text = (anchor.text + " " + data).strip()


def parse(html: str) -> _Builder:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder
