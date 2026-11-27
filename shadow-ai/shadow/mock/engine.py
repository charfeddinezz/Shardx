"""The mock browser: a CDP websocket that behaves like a patched ShardBrowser.

Commands implemented:

  Target.*     getTargets / attachToTarget / createTarget / activateTarget /
               closeTarget / setDiscoverTargets
  Runtime.*    evaluate / enable  (the SHADOW probe protocol + a few globals)
  Page.*       navigate / enable / captureScreenshot / getLayoutMetrics /
               getFrameTree / createIsolatedWorld
  DOM.enable
  Input.*      the non-human fallback, so tests can see when it is used
  Motion.*     the patched human-input domain

`Motion.enterText` really types: it takes a realistic duration per character
and writes into whichever element the mock considers focused. Events are
broadcast, as CDP does, so the Supervisor can be watched in a test.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import random
import threading
import time
import zlib
import struct
from typing import Any, Dict, List, Optional

from .dom import MockDocument
from .pages import page_for

PROBE_CALL = re.compile(r'window\.__shadowProbe\("(\w+)",\s*(\{.*\}|\[.*\]|null|"[^"]*")\s*\)', re.S)


class MockEngineSession:
    """One websocket = one browser."""

    def __init__(self, sock, profile_id: str, rng: random.Random, human_speed: float = 1.0,
                 motion: bool = True):
        self.motion = motion
        self.sock = sock
        self.profile_id = profile_id
        self.rng = rng
        self.human_speed = human_speed
        self.target_id = f"page-{next(_TARGETS)}"
        self.session_id = f"session-{next(_TARGETS)}"
        # One websocket can carry several page targets, as a browser does.
        self.docs: Dict[str, MockDocument] = {self.target_id: MockDocument("about:blank")}
        self.active = self.target_id
        # session history for the active page: back/forward are real CDP verbs
        self.history: List[str] = []
        self.history_index = -1
        self.pointer: Optional[Dict[str, float]] = None
        self.closed = False
        self.commands: Dict[str, int] = {}
        self._write_lock = threading.Lock()
        self._load("about:blank", page_for("about:blank")["html"])

    # -- io -----------------------------------------------------------
    @property
    def doc(self) -> MockDocument:
        return self.docs[self.active]

    def send(self, payload: dict) -> None:
        data = json.dumps(payload)
        with self._write_lock:
            try:
                self.sock.send_text(data)
            except Exception:
                self.closed = True

    def event(self, method: str, params: Optional[dict] = None, session: bool = True) -> None:
        payload: Dict[str, Any] = {"method": method, "params": params or {}}
        if session:
            payload["sessionId"] = self.session_id
        self.send(payload)

    def run(self) -> None:
        while not self.closed:
            try:
                raw = self.sock.recv_text()
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "id" not in msg:
                continue
            method = msg.get("method", "")
            self.commands[method] = self.commands.get(method, 0) + 1
            try:
                result = self.dispatch(method, msg.get("params") or {})
                self.send({"id": msg["id"], "result": result, "sessionId": msg.get("sessionId", self.session_id)})
            except KeyError:
                self.send({"id": msg["id"], "error": {"code": -32601, "message": f"'{method}' wasn't found"}})
            except Exception as exc:  # noqa: BLE001 - reported to the client
                self.send({"id": msg["id"], "error": {"code": -32000, "message": str(exc)}})
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass

    # -- dispatch -----------------------------------------------------
    def dispatch(self, method: str, params: dict) -> dict:
        if method == "Target.setDiscoverTargets":
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [self._target_info(target, doc) for target, doc in self.docs.items()]}
        if method == "Target.attachToTarget":
            target = params.get("targetId", self.active)
            if target not in self.docs:
                raise RuntimeError(f"no target {target}")
            self.active = target
            self.session_id = f"session-{next(_TARGETS)}"
            return {"sessionId": self.session_id}
        if method == "Target.createTarget":
            url = params.get("url", "about:blank")
            return {"targetId": self._open_target(url)}
        if method == "Target.closeTarget":
            target = params.get("targetId", "")
            if target in self.docs and target != self.target_id:
                del self.docs[target]
                if self.active == target:
                    self.active = self.target_id
                self.event("Target.targetDestroyed", {"targetId": target}, session=False)
            return {"success": True}
        if method == "Page.handleJavaScriptDialog":
            dialog = self.doc.dialog
            self.doc.dialog = None
            self.event("Page.javascriptDialogClosed", {"targetId": self.active, "result": bool(params.get("accept"))})
            return {"ok": bool(dialog)}
        if method in ("Target.activateTarget", "Target.closeTarget", "Target.detachFromTarget"):
            return {}
        if method in ("Runtime.enable", "Page.enable", "DOM.enable", "Network.enable"):
            return {}
        if method == "Page.reload":
            # a reload keeps the address and the dialog-free state, as a real one does
            self.event("Page.frameNavigated", {"frame": {"id": "frame-1", "url": self.doc.url}})
            self.doc.load(self.doc.url, page_for(self.doc.url)["html"])
            self.event("Page.loadEventFired", {"timestamp": time.time()})
            return {}
        if method == "Runtime.evaluate":
            return self._evaluate(params)
        if method == "Page.navigate":
            return self._navigate(params)
        if method == "Page.getLayoutMetrics":
            height = max(700, len(self.doc.nodes) * 20)
            return {"cssLayoutViewport": {"clientWidth": 1280, "clientHeight": 800},
                    "cssContentSize": {"width": 1280, "height": height}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1", "url": self.doc.url}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 1}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self._png()).decode()}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": None}}
        if method.startswith("Motion."):
            if not self.motion:
                raise KeyError(method)   # a build without the patched domain
            return self._motion(method.split(".", 1)[1], params)
        if method.startswith("Input."):
            return self._input(method, params)
        if method in ("Browser.getVersion", "SystemInfo.getInfo"):
            return {"product": "ShardBrowser/158.0.8127.36 (mock)", "protocolVersion": "1.3"}
        raise KeyError(method)

    def _target_info(self, target: Optional[str] = None, doc: Optional[MockDocument] = None) -> dict:
        target = target or self.active
        doc = doc or self.docs[target]
        return {"targetId": target, "type": "page", "title": doc.title,
                "url": doc.url, "attached": False,
                "openerId": self.target_id if target != self.target_id else ""}

    def _open_target(self, url: str) -> str:
        """A popup: a new page target, created by the page, as Chrome does."""
        target = f"page-{next(_TARGETS)}"
        doc = MockDocument(url)
        page = page_for(url)
        doc.load(url, page["html"])
        self.docs[target] = doc
        self.event("Target.targetCreated", {"targetInfo": {
            "targetId": target, "type": "page", "title": doc.title, "url": doc.url,
            "openerId": self.active, "attached": False}}, session=False)
        return target

    # -- page ---------------------------------------------------------
    def _load(self, url: str, html: str) -> None:
        self.doc.load(url, html)

    def _navigate(self, params: dict) -> dict:
        url = params.get("url", "about:blank")
        page = page_for(url)
        self.event("Page.frameNavigated", {"frame": {"id": "frame-1", "url": url}})
        self._load(url, page["html"])
        self.pointer = None
        # A real page has a session history; without one, back/forward are
        # untestable and a resumed run cannot tell where it is going back to.
        if not self.history or self.history[-1] != url:
            self.history.append(url)
            self.history_index = len(self.history) - 1
        self.event("Page.loadEventFired", {"timestamp": time.time()})
        return {"frameId": "frame-1"}

    def _evaluate(self, params: dict) -> dict:
        expr = (params.get("expression") or "").strip()
        if self.doc.dialog is not None and "__shadowProbe" in expr or (
                self.doc.dialog is not None and expr in ("document.readyState",)):
            # A real page blocks on an open dialog: scripts do not run.
            raise RuntimeError("Execution context was destroyed: a JavaScript dialog is open")
        self.doc.tick()
        probe_call = PROBE_CALL.search(expr)
        if probe_call:
            name = probe_call.group(1)
            try:
                arg = json.loads(probe_call.group(2))
            except ValueError:
                arg = None
            return self._value(self.doc.probe(name, arg))
        if "__shadowProbe" in expr and "window.__shadowProbe =" in expr:
            return self._value("ready")
        if expr in ("document.readyState",):
            return self._value("complete")
        if expr == "document.title":
            return self._value(self.doc.title)
        if expr == "location.href":
            return self._value(self.doc.url)
        if expr.startswith("history.back"):
            self._history(-1)
            return self._value(None)
        if expr.startswith("history.forward"):
            self._history(1)
            return self._value(None)
        if "el.options" in expr and "el.value" in expr:
            return self._value(self._select(expr))
        if "insertAdjacentHTML" in expr:
            for literal in _literals(expr, "insertAdjacentHTML"):
                self.doc.append_html(literal)
            return self._value(None)
        if "querySelectorAll" in expr and "getBoundingClientRect" in expr:
            # the DOM reader being (re)installed with its script, as a re-attach
            # after a dialog does on a real page
            return self._value("ok")
        if "querySelector" in expr and ".focus()" in expr:
            for selector in _literals(expr, "querySelector"):
                node = self.doc.by_id(selector[1:]) if selector.startswith("#") else None
                if node is None:
                    hits = self.doc.query(selector)
                    node = hits[0] if hits else None
                if node is not None:
                    self.doc.focus_id = node.id or self.doc.focus_id
            return self._value(True)
        if expr.startswith("performance.now"):
            return self._value(1.0)
        if "getBoundingClientRect" in expr:
            return self._value({"x": 0, "y": 0, "width": 1280, "height": 800})
        return self._value(None)

    def _history(self, delta: int) -> None:
        index = self.history_index + delta
        if 0 <= index < len(self.history):
            self.history_index = index
            url = self.history[index]
            self.event("Page.frameNavigated", {"frame": {"id": "frame-1", "url": url}})
            self.doc.load(url, page_for(url)["html"])
            self.pointer = None
            self.event("Page.loadEventFired", {"timestamp": time.time()})

    def _select(self, expr: str) -> bool:
        """The one evaluate() shadow/engines/actions.py uses to pick an option."""
        selector = next(iter(_literals(expr, "querySelector")), "")
        wanted = next((lit for lit in _literals(expr, "includes") if lit), "").lower()
        node = self.doc.query(selector)[0] if selector and self.doc.query(selector) else None
        if node is None:
            return False
        for option in node.children if hasattr(node, "children") else []:
            text = option.inner_text().strip().lower()
            if wanted and wanted in text:
                self.doc.values[node.id or "select"] = option.get("value") or text or wanted
                self.doc.focus_id = node.id or self.doc.focus_id
                self.doc.mutations += 1
                return True
        return False

    @staticmethod
    def _value(value: Any) -> dict:
        return {"result": {"type": "object", "value": value}}

    def _png(self) -> bytes:
        """A tiny solid PNG, so the Vision Engine sees a real image."""
        width, height = 320, 200
        seed = zlib.crc32(self.doc.url.encode()) & 0xFF
        rows = b"".join(b"\x00" + bytes([seed, (seed * 3) % 256, 200]) * width for _ in range(height))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data +
                    struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
                chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    # -- human input --------------------------------------------------
    def _motion(self, name: str, params: dict) -> dict:
        if name == "createPointer":
            self.pointer = {"x": float(params.get("x", 0)), "y": float(params.get("y", 0))}
            self.event("Motion.pointerCreated", {"x": self.pointer["x"], "y": self.pointer["y"]}, session=False)
            return {}
        if name not in ("glideTo", "tap", "dragTo", "wheel", "enterText", "pressKey", "destroyPointer", "touchTap"):
            raise KeyError(f"Motion.{name}")
        if self.pointer is None and name != "destroyPointer":
            raise RuntimeError("no pointer: call Motion.createPointer first")

        if name == "destroyPointer":
            self.pointer = None
            return {}
        if name in ("glideTo", "dragTo"):
            target = {"x": float(params.get("x", 0)), "y": float(params.get("y", 0))}
            target_width = float(params.get("targetWidth", 32) or 32)
            start = dict(self.pointer or {"x": 0, "y": 0})
            distance = ((target["x"] - start["x"]) ** 2 + (target["y"] - start["y"]) ** 2) ** 0.5
            duration = int(max(60, min(1400, 80 + distance * 0.55 + (32 / max(8, target_width)) * 90)))
            duration = int(duration * self.human_speed)
            steps = 12
            for i in range(1, steps + 1):
                t = i / steps
                ease = t * t * (3 - 2 * t)
                self.pointer = {"x": start["x"] + (target["x"] - start["x"]) * ease,
                                "y": start["y"] + (target["y"] - start["y"]) * ease}
                self.event("Motion.pointerMoved", self.pointer, session=False)
            if name == "dragTo":
                self.event("Motion.dragStart", target, session=False)
            time.sleep(min(0.25, duration / 1000.0))
            self.event("Motion.glideComplete" if name == "glideTo" else "Motion.dragComplete",
                       {"x": self.pointer["x"], "y": self.pointer["y"]}, session=False)
            return {"durationMs": duration}
        if name in ("tap", "touchTap"):
            click_count = int(params.get("clickCount", params.get("tapCount", 1)) or 1)
            node = self._node_at(self.pointer or {"x": 0, "y": 0})
            if node is not None:
                self._activate(node)
            self.event("Motion.tapPerformed", {"x": (self.pointer or {}).get("x", 0),
                                               "y": (self.pointer or {}).get("y", 0),
                                               "target": node.tag if node else None}, session=False)
            return {"durationMs": int(70 * click_count * self.human_speed)}
        if name == "wheel":
            dy = float(params.get("deltaY", 0) or 0)
            self.doc.scroll_y = max(0.0, self.doc.scroll_y + dy)
            self.event("Motion.wheelScrolled", {"deltaY": dy}, session=False)
            return {"durationMs": 120}
        if name == "enterText":
            text = str(params.get("text", ""))
            duration = int(max(80, len(text) * 70 * self.human_speed))
            key = self.doc.focus_id
            if key:
                self.doc.values[key] = (self.doc.values.get(key, "") + text)
                self.doc.mutations += 1
                node = self.doc.by_id(key)
                self.event("Motion.textEntered", {"id": key, "length": len(text),
                                                  "name": node.get("name") if node else ""}, session=False)
            time.sleep(min(0.3, duration / 1000.0))
            return {"durationMs": duration}
        if name == "pressKey":
            pressed = str(params.get("key", ""))
            if pressed == "Tab":
                self._focus_next()
            if pressed == "Enter":
                focused = self.doc.by_id(self.doc.focus_id) if self.doc.focus_id else None
                form = None
                if focused is not None:
                    form = next((a for a in focused.ancestors() if a.tag == "form"), None)
                if form is not None:
                    self._submit(form)
            self.event("Motion.keyPressed", {"key": pressed}, session=False)
            return {"durationMs": 45}
        raise KeyError(name)

    def _input(self, method: str, params: dict) -> dict:
        self.event("Input.fallbackUsed", {"method": method}, session=False)
        if method == "Input.insertText":
            key = self.doc.focus_id
            if key:
                self.doc.values[key] = self.doc.values.get(key, "") + str(params.get("text", ""))
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
            node = self._node_at({"x": float(params.get("x", 0)), "y": float(params.get("y", 0))})
            if node is not None:
                self._activate(node)
        return {}

    # -- element behaviour --------------------------------------------
    def _node_at(self, point: dict) -> Optional[Any]:
        x, y = float(point.get("x", 0)), float(point.get("y", 0))
        best = None
        for node in self.doc.nodes:
            info = self.doc.describe(node)
            rect = info["rect"]
            if rect["width"] <= 0 or rect["height"] <= 0:
                continue
            if rect["x"] <= x <= rect["x"] + rect["width"] and rect["y"] <= y <= rect["y"] + rect["height"]:
                if best is None or rect["height"] < best[0]:
                    best = (rect["height"], node)
        return best[1] if best else None

    def _focus_next(self) -> None:
        fields = [n for n in self.doc.nodes if n.tag in ("input", "textarea", "select", "button")]
        if not fields:
            return
        keys = [n.id for n in fields if n.id]
        if not keys:
            return
        current = self.doc.focus_id
        idx = keys.index(current) + 1 if current in keys else 0
        self.doc.focus_id = keys[idx % len(keys)]
        self.event("Runtime.consoleAPICalled", {"type": "log", "args": [{"value": f"focus={self.doc.focus_id}"}]})

    def _activate(self, node) -> None:
        confirm = node.get("data-confirm")
        if confirm:
            self.doc.dialog = {"type": "confirm", "message": confirm}
            self.event("Page.javascriptDialogOpening",
                       {"type": "confirm", "message": confirm, "targetId": self.active})
            return
        if node.tag == "a" and node.get("target") == "_blank":
            href = node.get("href", "")
            if href.startswith("/"):
                href = "/".join(self.doc.url.split("/")[:3]) + href
            self._open_target(href)
            return
        if node.tag in ("input", "textarea", "select"):
            self.doc.focus_id = node.id or self.doc.focus_id
            return
        if node.tag == "button":
            form = next((a for a in node.ancestors() if a.tag == "form"), None)
            if form is not None:
                self._submit(form)
            return
        if node.tag == "a":
            href = node.get("href")
            if href:
                if href.startswith("/"):
                    parts = self.doc.url.split("/")
                    href = "/".join(parts[:3]) + href
                self._navigate({"url": href})

    def _submit(self, form) -> None:
        action = form.get("action") or "/"
        if action.startswith("/"):
            parts = self.doc.url.split("/")
            action = "/".join(parts[:3]) + action
        query = []
        for field in form.walk():
            if field.tag in ("input", "textarea", "select") and field.get("name"):
                key = field.id or f"@{field.tag}"
                query.append(f"{field.get('name')}={self.doc.values.get(key, '')}")
        if query:
            action += "?" + "&".join(query)
        self._navigate({"url": action})


_TARGETS = itertools.count(1)


def _literals(expression: str, call: str) -> List[str]:
    """Pull the string arguments out of `call(...)` in a script, without eval."""
    import ast
    out: List[str] = []
    index = 0
    while True:
        at = expression.find(call + "(", index)
        if at < 0:
            return out
        depth, i = 0, at + len(call)
        while i < len(expression):
            if expression[i] == "(":
                depth += 1
            elif expression[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = expression[at + len(call) + 1:i]
        for piece in inner.split(","):
            piece = piece.strip()
            try:
                value = ast.literal_eval(piece)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str):
                out.append(value)
        index = i + 1
