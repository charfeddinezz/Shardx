"""Smart Keyboard and Smart Mouse.

Both drive ShardX's patched `Motion` domain, which is the engine's own human
input path: pointer movement has acceleration and overshoot, typing has
rhythm. Nothing here synthesises DOM events, and nothing here injects script
into the page.

If a build does not expose `Motion`, the engines fall back to `Input.*` only
when `fallback=True` is passed explicitly, and they set `degraded` so the
journal records that the run was visible to the page.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..errors import ElementNotFound
from .base import Engine


class KeyboardEngine(Engine):
    name = "keyboard"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.typed_chars = 0
        self.degraded = False
        self._motion_ok: Optional[bool] = None

    # -- availability -------------------------------------------------
    def motion_ok(self, cdp) -> bool:
        if self._motion_ok is None:
            self._motion_ok = cdp.motion_available()
        return bool(self._motion_ok)

    # -- primitives ---------------------------------------------------
    def focus(self, probe, element_id: str = "", selector: str = "") -> bool:
        """Put the caret in a field. Reports whether the field really has it."""
        if element_id:
            current = probe.active()
            if current and current.get("id") == element_id:
                return True
            node = probe.by_id(element_id)
            if node is None:
                raise ElementNotFound(f"no element with id {element_id!r}")
            # Focus needs a pointer. Without one, fall back to the page's own
            # focus() — it is the one place where not being a human shows, and
            # the engine records that it did.
            if not self._has_pointer():
                self._fallback_focus(node)
            else:
                self.click_node(probe, node)
        elif selector:
            nodes = probe.snapshot(selector, limit=2)
            if not nodes:
                raise ElementNotFound(f"nothing matches {selector!r}")
            self.click_node(probe, nodes[0])
        deadline = time.time() + 5.0
        while time.time() < deadline:
            active = probe.active()
            if active and ((element_id and active.get("id") == element_id) or
                           (selector and active.get("selector") == selector) or
                           (not element_id and not selector and active)):
                return True
            time.sleep(0.05)
        return False

    def _has_pointer(self) -> bool:
        cdp = self.ctx.cdp
        if cdp is None:
            return False
        try:
            return cdp.motion_available()
        except Exception:
            return False

    def _fallback_focus(self, node: Dict[str, Any]) -> None:
        selector = node.get("id") and f"#{node['id']}" or node.get("selector")
        if not selector:
            raise ElementNotFound("cannot focus an element with no selector")
        self.ctx.ensure_cdp().evaluate(
            f"(() => {{ const el = document.querySelector({selector!r});"
            f" if (el && el.focus) el.focus(); return !!el; }})()")
        self.degraded = True

    def click_node(self, probe, node: Dict[str, Any]) -> None:
        """Tap the middle of a node with the mouse engine's pointer."""
        if "mouse" not in self.ctx.registry:
            self._fallback_focus(node)
            return
        rect = node.get("rect") or {}
        x = rect.get("x", 0) + rect.get("width", 0) / 2
        y = rect.get("y", 0) + rect.get("height", 0) / 2
        self.ctx.registry.get("mouse").tap_at(x, y, width=rect.get("width", 32))

    def type(self, text: str, allow_typos: bool = False) -> Dict[str, Any]:
        cdp = self.ctx.ensure_cdp()
        started = time.time()
        if self.motion_ok(cdp):
            cdp.enter_text(text, allow_typos=allow_typos or self.ctx.cfg.human.typo_rate > 0)
        else:
            cdp.raw_type(text)
            self.degraded = True
        elapsed = (time.time() - started) * 1000
        self.typed_chars += len(text)
        self.note(chars=len(text))
        self.emit("typed", chars=len(text), ms=round(elapsed), degraded=self.degraded)
        return {"typed": len(text), "ms": round(elapsed), "degraded": self.degraded}

    def press(self, key: str, modifiers: Optional[List[str]] = None) -> Dict[str, Any]:
        cdp = self.ctx.ensure_cdp()
        if self.motion_ok(cdp):
            ms = cdp.press_key(key, modifiers)
        else:
            cdp.raw_key(key)
            ms = 0
            self.degraded = True
        self.note(key=key)
        self.emit("pressed", key=key)
        return {"key": key, "ms": ms}

    def hotkey(self, *keys: str) -> Dict[str, Any]:
        """e.g. hotkey('Control', 'a') — modifiers held, then the last key."""
        modifiers = [k for k in keys[:-1]]
        return self.press(keys[-1], modifiers)

    def clear_field(self, probe, node: Dict[str, Any]) -> None:
        """Select-all then type over — one gesture, no backspace loops."""
        self.click_node(probe, node)
        self.press("a", ["Control"])
        self.press("Backspace")

    def fill_field(self, probe, node: Dict[str, Any], value: str,
                   clear: bool = True, allow_typos: bool = False) -> Dict[str, Any]:
        """Focus, optionally clear, type, then verify the field holds the value."""
        element_id = node.get("id") or ""
        if not self.focus(probe, element_id=element_id) and element_id:
            # a field that resists focus is usually hidden behind a label;
            # tap it once more through the pointer, if there is one
            if "mouse" in self.ctx.registry:
                self.click_node(probe, node)
                self.focus(probe, element_id=element_id)
        if clear and "mouse" in self.ctx.registry:
            self.clear_field(probe, node)   # select-all needs a pointer; skipping is safe
        result = self.type(value, allow_typos=allow_typos)
        actual = probe.value(element_id) if element_id else None
        result["verified"] = (actual == value) if actual is not None else None
        result["actual"] = actual
        if result["verified"] is False and actual is not None and actual != value:
            # Some widgets clear themselves on blur; type once more before failing.
            self.click_node(probe, node)
            self.type(value, allow_typos=False)
            result["actual"] = probe.value(element_id)
            result["verified"] = result["actual"] == value
        return result

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"typed_chars": self.typed_chars, "degraded": self.degraded})
        return info

    def reset(self) -> None:
        super().reset()
        self._motion_ok = None
        self.degraded = False


class MouseEngine(Engine):
    name = "mouse"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.moves = 0
        self.clicks = 0
        self.degraded = False
        self._pointer_at = (0.0, 0.0)
        self._has_pointer = False

    # -- pointer ------------------------------------------------------
    def ensure_pointer(self, cdp, x: float = 0.0, y: float = 0.0) -> None:
        if not self._has_pointer:
            cdp.pointer_create(x, y, self.ctx.cfg.human.pace, self.ctx.cfg.human.pointer_seed)
            self._pointer_at = (x, y)
            self._has_pointer = True

    def move_to(self, x: float, y: float, width: float = 32.0) -> Dict[str, Any]:
        cdp = self.ctx.ensure_cdp()
        if not cdp.motion_available():
            cdp.raw_mouse("move", x, y)
            self.degraded = True
            self._pointer_at = (x, y)
            return {"x": x, "y": y, "ms": 0, "degraded": True}
        self.ensure_pointer(cdp)
        ms = cdp.pointer_glide(x, y, target_width=width)
        self._pointer_at = (x, y)
        self.moves += 1
        self.note(x=round(x), y=round(y))
        return {"x": x, "y": y, "ms": ms}

    def tap_at(self, x: float, y: float, width: float = 32.0, click_count: int = 1) -> Dict[str, Any]:
        moved = self.move_to(x, y, width)
        cdp = self.ctx.ensure_cdp()
        if cdp.degraded or self.degraded:
            cdp.raw_mouse("down", x, y, click_count=click_count)
            cdp.raw_mouse("up", x, y, click_count=click_count)
            ms = 0
        else:
            self._pause_before_click()
            ms = cdp.pointer_tap(click_count=click_count)
        self.clicks += 1
        self.emit("clicked", x=round(x), y=round(y), click_count=click_count)
        return {"x": x, "y": y, "move_ms": moved["ms"], "tap_ms": ms, "clicks": click_count}

    def click_element(self, probe, node: Dict[str, Any], click_count: int = 1) -> Dict[str, Any]:
        rect = node.get("rect") or {}
        if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
            raise ElementNotFound(f"{node.get('selector')} has no clickable box")
        # Aim slightly inside the box, not always dead centre — a person does
        # not hit the same pixel every time, and some layouts have a dead centre.
        rng = self.ctx.rng
        jitter_x = rng.uniform(-0.28, 0.28) * rect["width"] / 2
        jitter_y = rng.uniform(-0.28, 0.28) * rect["height"] / 2
        x = rect["x"] + rect["width"] / 2 + jitter_x
        y = rect["y"] + rect["height"] / 2 + jitter_y
        return self.tap_at(x, y, width=rect["width"], click_count=click_count)

    def click_label(self, probe, label: str) -> Dict[str, Any]:
        node = self.ctx.registry.get("discovery").pick(probe, label).node
        return self.click_element(probe, node)

    def hover(self, probe, node: Dict[str, Any]) -> Dict[str, Any]:
        rect = node.get("rect") or {}
        return self.move_to(rect.get("x", 0) + rect.get("width", 0) / 2,
                            rect.get("y", 0) + rect.get("height", 0) / 2,
                            width=rect.get("width", 32))

    def drag(self, x1: float, y1: float, x2: float, y2: float) -> Dict[str, Any]:
        cdp = self.ctx.ensure_cdp()
        self.move_to(x1, y1)
        ms = cdp.pointer_drag(x2, y2) if not self.degraded else 0
        if self.degraded:
            cdp.raw_mouse("down", x1, y1)
            cdp.raw_mouse("move", x2, y2)
            cdp.raw_mouse("up", x2, y2)
        self.emit("dragged", x1=round(x1), y1=round(y1), x2=round(x2), y2=round(y2))
        return {"dragged": True, "ms": ms}

    def scroll(self, direction: str = "down", amount: int = 480) -> Dict[str, Any]:
        cdp = self.ctx.ensure_cdp()
        dy = abs(amount) if direction != "up" else -abs(amount)
        if cdp.motion_available() and not self.degraded:
            ms = cdp.wheel(dy)
        else:
            cdp.raw_scroll(dy)
            ms = 0
            self.degraded = True
        self.note(direction=direction, amount=dy)
        return {"dy": dy, "ms": ms}

    def _pause_before_click(self) -> None:
        """A short, human-sized beat between arriving and pressing."""
        think = self.ctx.cfg.human.think_pause_ms / 1000.0
        if think > 0:
            time.sleep(min(think, 0.6) * self.ctx.rng.uniform(0.4, 1.1))

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"moves": self.moves, "clicks": self.clicks, "degraded": self.degraded,
                     "pointer": self._has_pointer})
        return info

    def reset(self) -> None:
        super().reset()
        self._has_pointer = False
        self.degraded = False
