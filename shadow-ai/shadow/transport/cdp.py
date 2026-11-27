"""Chrome DevTools Protocol client.

Two channels are kept, on purpose:

* the **browser** channel (no session id) — that is where ShardX merges its
  patched `Motion` domain, which produces human pointer and key input;
* a **page** session (flat attach) — for the DOM, screenshots, navigation and
  reads.

Anything that writes to the page goes through `Motion` first. Only when a
build does not expose it does the client fall back to `Input.*`, and then it
says so in `self.degraded`, because that fallback is visible to a page.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..errors import CdpError, TransportError
from .http import HttpClient
from .ws import WsClient

# Domains that live on the browser channel, never inside a page session.
BROWSER_ONLY = {"Target", "Browser", "Motion", "SystemInfo", "Schema", "Tethering", "ServiceWorker"}
NOT_FOUND = (-32601, "wasn't found", "is not found", "not found", "was not found")


class CdpClient:
    def __init__(self, ws_url: str, timeout: float = 30.0, on_event: Optional[Callable[[dict], None]] = None):
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = WsClient(ws_url, timeout=timeout)
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._events: List[dict] = []
        self._on_event = on_event
        self._closed = False
        self.degraded = False
        self.commands: Dict[str, int] = {}
        self.session_id: Optional[str] = None
        self.target_id: Optional[str] = None

    # -- lifecycle ----------------------------------------------------
    @classmethod
    def from_http(cls, http_url: str, **kw) -> "CdpClient":
        """Discover the browser websocket from a profile's `http_url`."""
        client = HttpClient(http_url, timeout=10)
        version = client.json_get("/json/version")
        ws = (version or {}).get("webSocketDebuggerUrl")
        if not ws:
            listing = client.json_get("/json/list") or []
            for entry in listing:
                if entry.get("webSocketDebuggerUrl") and entry.get("type") == "browser":
                    ws = entry["webSocketDebuggerUrl"]
                    break
        if not ws:
            raise TransportError(f"no browser websocket at {http_url}/json/version")
        return cls(ws, **kw)

    def connect(self) -> "CdpClient":
        self._ws.connect()
        self.call("Target.setDiscoverTargets", {"discover": True}, timeout=10)
        return self

    def close(self) -> None:
        self._closed = True
        self._ws.close()

    def __enter__(self) -> "CdpClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- raw calls ----------------------------------------------------
    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             session_id: Any = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one command and wait for its reply.

        `session_id`:
          * ``None`` (default) — route automatically: browser-level commands
            (see ``BROWSER_ONLY``) go on the root channel, everything else on
            the page session when one is attached.
          * ``""`` — force the root channel (this is how ``Motion`` is called).
          * a string — use that session.
        """
        if self._closed:
            raise TransportError("CDP session is closed")
        msg: Dict[str, Any] = {"id": next(self._ids), "method": method, "params": params or {}}
        if session_id == "":
            pass
        elif session_id:
            msg["sessionId"] = session_id
        elif self.session_id and method.split(".")[0] not in BROWSER_ONLY:
            msg["sessionId"] = self.session_id
        deadline = time.time() + (timeout or self.timeout)
        with self._lock:
            try:
                self._ws.send_text(json.dumps(msg))
            except TransportError:
                self._closed = True
                raise
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TransportError(f"CDP {method} timed out after {timeout or self.timeout}s")
                self._wsTimeout(remaining)
                try:
                    raw = self._ws.recv_text()
                except TransportError:
                    self._closed = True
                    raise
                payload = json.loads(raw)
                if payload.get("id") == msg["id"]:
                    self.commands[method] = self.commands.get(method, 0) + 1
                    if "error" in payload:
                        err = payload["error"] or {}
                        raise CdpError(err.get("message", "CDP error"), code_name=err.get("code"), method=method)
                    return payload.get("result", {}) or {}
                if "method" in payload:
                    self._dispatch(payload)

    def _wsTimeout(self, seconds: float) -> None:
        sock = getattr(self._ws, "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(max(0.05, min(seconds, 60)))
            except Exception:
                pass

    def _dispatch(self, payload: dict) -> None:
        self._events.append(payload)
        if len(self._events) > 2000:
            del self._events[:1000]
        if self._on_event:
            try:
                self._on_event(payload)
            except Exception:
                pass

    def drain_events(self) -> List[dict]:
        out, self._events = self._events, []
        return out

    # -- targets ------------------------------------------------------
    def get_targets(self) -> List[dict]:
        return self.call("Target.getTargets", timeout=10).get("targetInfos", [])

    def page_target(self) -> Optional[dict]:
        pages = [t for t in self.get_targets() if t.get("type") == "page" and not t.get("attached")]
        if pages:
            return pages[0]
        pages = [t for t in self.get_targets() if t.get("type") == "page"]
        return pages[0] if pages else None

    def attach_page(self, target_id: Optional[str] = None) -> str:
        target = self.call("Target.getTargets", timeout=10)
        pages = [t for t in target.get("targetInfos", []) if t.get("type") == "page"]
        if not pages:
            raise CdpError("the browser has no page target")
        chosen = target_id or pages[0]["targetId"]
        result = self.call("Target.attachToTarget", {"targetId": chosen, "flatten": True}, timeout=15)
        self.session_id = result.get("sessionId")
        self.target_id = chosen
        if not self.session_id:
            raise CdpError("attachToTarget returned no sessionId")
        self.call("Page.enable", {}, timeout=10)
        self.call("Runtime.enable", {}, timeout=10)
        try:
            self.call("DOM.enable", {}, timeout=10)
        except CdpError:
            pass
        return self.session_id

    def new_page(self, url: str = "about:blank") -> dict:
        return self.call("Target.createTarget", {"url": url}, timeout=20)

    def activate(self, target_id: Optional[str] = None) -> None:
        tid = target_id or self.target_id
        if tid:
            try:
                self.call("Target.activateTarget", {"targetId": tid}, timeout=5)
            except CdpError:
                pass

    def close_page(self, target_id: Optional[str] = None) -> None:
        tid = target_id or self.target_id
        if tid:
            self.call("Target.closeTarget", {"targetId": tid}, timeout=10)

    # -- page operations ----------------------------------------------
    def navigate(self, url: str, wait: bool = True, timeout: float = 45.0) -> Dict[str, Any]:
        self.attach_if_needed()
        res = self.call("Page.navigate", {"url": url}, timeout=timeout)
        error = res.get("errorText")
        if error:
            raise CdpError(f"navigation refused: {error}", url=url)
        if wait:
            self.wait_ready(timeout=timeout)
        return res

    def attach_if_needed(self) -> str:
        if not self.session_id:
            self.attach_page()
        return self.session_id or ""

    def wait_ready(self, timeout: float = 30.0, settle: float = 0.25) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.evaluate("document.readyState")
            except (CdpError, TransportError):
                time.sleep(0.2)
                continue
            if state in ("complete", "interactive"):
                time.sleep(settle)
                return True
            time.sleep(0.15)
        return False

    def evaluate(self, expression: str, await_promise: bool = True,
                 by_value: bool = True, timeout: Optional[float] = None) -> Any:
        self.attach_if_needed()
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": by_value,
            "userGesture": False,
        }, timeout=timeout or 30.0)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text", "page error")
            raise CdpError(f"page evaluation failed: {text.splitlines()[0]}", expression=expression[:200])
        remote = result.get("result", {})
        if by_value:
            return remote.get("value")
        return remote

    def call_function(self, function_declaration: str, args: Iterable[Any] = (), timeout: float = 20.0) -> Any:
        self.attach_if_needed()
        result = self.call("Runtime.callFunctionOn", {
            "functionDeclaration": function_declaration,
            "executionContextId": self._execution_context(),
            "arguments": [{"value": a} for a in args],
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            text = ((result["exceptionDetails"].get("exception") or {}).get("description") or "page error")
            raise CdpError(f"page function failed: {text.splitlines()[0]}")
        return (result.get("result") or {}).get("value")

    def _execution_context(self) -> int:
        tree = self.call("Page.getFrameTree", timeout=10)
        frame_id = (tree.get("frameTree") or {}).get("frame", {}).get("id")
        world = self.call("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "shadow"}, timeout=10)
        return int(world.get("executionContextId", 0)) or 1

    def screenshot(self, fmt: str = "png", quality: Optional[int] = None,
                   full_page: bool = False) -> bytes:
        import base64
        self.attach_if_needed()
        params: Dict[str, Any] = {"format": fmt, "captureBeyondViewport": full_page}
        if quality is not None and fmt in ("jpeg", "webp"):
            params["quality"] = int(quality)
        result = self.call("Page.captureScreenshot", params, timeout=40)
        data = result.get("data")
        if not data:
            raise CdpError("screenshot returned no data")
        return base64.b64decode(data)

    def viewport(self) -> Dict[str, int]:
        metrics = self.call("Page.getLayoutMetrics", timeout=10)
        css = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
        return {"width": int(css.get("clientWidth", 0)), "height": int(css.get("clientHeight", 0)),
                "page_width": int((metrics.get("cssContentSize") or {}).get("width", 0)),
                "page_height": int((metrics.get("cssContentSize") or {}).get("height", 0))}

    def current_url(self) -> str:
        return self.evaluate("location.href") or ""

    def title(self) -> str:
        return self.evaluate("document.title") or ""

    # -- ShardX Motion domain (human input) ---------------------------
    def motion_available(self) -> bool:
        """True when the build exposes the patched Motion domain.

        `createPointer` is the probe: an engine without the domain answers
        "method wasn't found", which is the only answer that means absent. A
        pointer that already exists is still an available domain.
        """
        try:
            self.call("Motion.createPointer", {"x": 0, "y": 0}, session_id="", timeout=10)
            return True
        except CdpError as exc:
            text = str(exc).lower()
            if exc.details.get("code_name") == -32601 or any(t in text for t in NOT_FOUND):
                self.degraded = True
                return False
            return True
        except TransportError:
            raise

    def motion(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
        return self.call(f"Motion.{method}", params or {}, session_id="", timeout=timeout)

    def pointer_create(self, x: float, y: float, pace_scale: float = 1.0,
                       seed: Optional[int] = None) -> None:
        params: Dict[str, Any] = {"x": x, "y": y, "paceScale": pace_scale}
        if seed is not None:
            params["seed"] = int(seed)
        self.motion("createPointer", params)

    def pointer_glide(self, x: float, y: float, target_width: float = 32.0) -> int:
        return int(self.motion("glideTo", {"x": x, "y": y, "targetWidth": target_width}).get("durationMs", 0))

    def pointer_tap(self, button: str = "left", click_count: int = 1) -> int:
        return int(self.motion("tap", {"button": button, "clickCount": click_count}).get("durationMs", 0))

    def pointer_drag(self, x: float, y: float, target_width: float = 32.0) -> int:
        return int(self.motion("dragTo", {"x": x, "y": y, "targetWidth": target_width}).get("durationMs", 0))

    def wheel(self, dy: float, dx: float = 0.0) -> int:
        return int(self.motion("wheel", {"deltaY": dy, "deltaX": dx}).get("durationMs", 0))

    def enter_text(self, text: str, allow_typos: bool = False) -> int:
        return int(self.motion("enterText", {"text": text, "allowTypos": allow_typos}, timeout=180).get("durationMs", 0))

    def press_key(self, key: str, modifiers: Optional[List[str]] = None) -> int:
        return int(self.motion("pressKey", {"key": key, "modifiers": modifiers or []}).get("durationMs", 0))

    def pointer_destroy(self) -> None:
        try:
            self.motion("destroyPointer", timeout=10)
        except CdpError:
            pass

    def touch_tap(self, x: float, y: float, target_width: float = 32.0, tap_count: int = 1) -> int:
        return int(self.motion("touchTap", {"x": x, "y": y, "targetWidth": target_width,
                                            "tapCount": tap_count}).get("durationMs", 0))

    # -- fallback input (visible to the page; only when Motion is absent) --
    def raw_mouse(self, kind: str, x: float, y: float, button: str = "left", click_count: int = 1) -> None:
        self.degraded = True
        typ = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}[kind]
        params: Dict[str, Any] = {"type": typ, "x": x, "y": y, "button": button,
                                  "buttons": 1 if typ == "mousePressed" else 0, "clickCount": click_count}
        self.call("Input.dispatchMouseEvent", params, timeout=10)

    def raw_type(self, text: str) -> None:
        self.degraded = True
        self.call("Input.insertText", {"text": text}, timeout=30)

    def raw_key(self, key: str, code: str = "", key_code: int = 0) -> None:
        self.degraded = True
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": code,
                                             "windowsVirtualKeyCode": key_code}, timeout=10)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                             "windowsVirtualKeyCode": key_code}, timeout=10)

    def raw_scroll(self, dy: float, dx: float = 0.0) -> None:
        self.degraded = True
        self.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 0, "y": 0,
                                               "deltaX": dx, "deltaY": dy}, timeout=10)

    # -- observers ----------------------------------------------------
    def settle(self, quiet_ms: int = 350, max_ms: int = 5000) -> bool:
        """Wait until the page stops changing (DOM mutations, network-ish)."""
        script = """
        (function(quiet, maxWait){
          return new Promise((resolve) => {
            const start = Date.now();
            let last = Date.now();
            let timer = null;
            const obs = new MutationObserver(() => { last = Date.now(); });
            try { obs.observe(document.documentElement, {subtree:true, childList:true, attributes:true}); } catch (e) {}
            const tick = () => {
              const now = Date.now();
              if (now - last >= quiet || now - start >= maxWait) { obs.disconnect(); resolve(true); return; }
              timer = setTimeout(tick, 50);
            };
            tick();
          });
        })(%d, %d)
        """ % (quiet_ms, max_ms)
        try:
            return bool(self.evaluate(script, timeout=(max_ms / 1000.0) + 10))
        except (CdpError, TransportError):
            return False
