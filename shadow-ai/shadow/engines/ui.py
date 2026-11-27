"""Smart UI — state, notifications and the live line.

Two surfaces: a compact text ticker for a terminal, and a state document a
local dashboard can read (`state('/path')` writes it atomically). Every
notification is also an event, so the journal has it even if no dashboard is
open.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

from ..util import iso, now, truncate
from .base import Engine

LEVEL_MARK = {"info": "·", "ok": "+", "warn": "!", "error": "x", "action": ">"}


@dataclass
class Notice:
    level: str
    text: str
    ts: float = field(default_factory=now)
    data: Dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return f"{LEVEL_MARK.get(self.level, '·')} {truncate(self.text, 160)}"


class UiEngine(Engine):
    name = "ui"

    def __init__(self, ctx, printer: Optional[Callable[[str], None]] = None):
        super().__init__(ctx)
        self.notices: Deque[Notice] = deque(maxlen=200)
        self.lines: Deque[str] = deque(maxlen=400)
        self._printer = printer or (lambda line: print(line, flush=True))
        self._state_path: Optional[Path] = None
        self._lock = threading.RLock()
        self.quiet = False
        self.subscribed = False

    def start(self) -> None:
        super().start()
        if not self.subscribed:
            self.ctx.bus.on("task.*", self._on_event)
            self.ctx.bus.on("engine.*", self._on_engine_event)
            self.ctx.bus.on("safety.*", self._on_event)
            self.subscribed = True

    # -- notifications ------------------------------------------------
    def notify(self, level: str, text: str, **data: Any) -> Notice:
        notice = Notice(level=level, text=text, data=data)
        with self._lock:
            self.notices.append(notice)
        payload = {k: v for k, v in data.items() if k not in ("level", "text", "topic", "source")}
        self.ctx.bus.publish("ui.notice", source="ui", level=level, text=text, **payload)
        self.render(notice.render())
        return notice

    def render(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
        if not self.quiet:
            try:
                self._printer(line)
            except Exception:
                pass

    def banner(self, title: str, subtitle: str = "") -> None:
        width = 66
        self.render("┌" + "─" * width + "┐")
        self.render("│ " + title[:width - 2].ljust(width - 2) + " │")
        if subtitle:
            self.render("│ " + subtitle[:width - 2].ljust(width - 2) + " │")
        self.render("└" + "─" * width + "┘")

    # -- events -------------------------------------------------------
    def _on_event(self, event) -> None:
        data = event.data or {}
        level = {"task.started": "action", "task.finished": "ok", "task.failed": "error",
                 "step.started": "action", "step.done": "ok", "step.failed": "error",
                 "task.recovered": "warn"}.get(event.topic, "info")
        if event.topic in ("step.started", "step.done", "step.failed", "task.started",
                           "task.finished", "task.failed", "task.cancelled"):
            return  # the workflow prints its own step and task lines
        text = data.get("text") or data.get("goal") or data.get("action") or ""
        if not text:
            return
        extra = {k: v for k, v in data.items() if k not in ("text", "topic", "level", "source")}
        self.notify(level, str(text), **extra)

    def _on_engine_event(self, event) -> None:
        if event.topic in ("engine.started", "engine.failed", "engine.restarted"):
            level = {"engine.started": "info", "engine.failed": "error", "engine.restarted": "warn"}[event.topic]
            self.notify(level, f"{event.data.get('name', '?')}: {event.topic.split('.')[-1]}",
                        **{k: v for k, v in event.data.items() if k != "name"})

    # -- state document -----------------------------------------------
    def state(self, path: Optional[str] = None) -> Dict[str, Any]:
        if path:
            self._state_path = Path(path)
        snapshot = {
            "ts": iso(),
            "engine": "ui",
            "notices": [{"level": n.level, "text": n.text, "ts": iso(n.ts)} for n in list(self.notices)[-50:]],
            "lines": list(self.lines)[-120:],
            "context": self.ctx.snapshot(),
            "engines": self.ctx.registry.health(),
        }
        if self._state_path is not None:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, self._state_path)
            except OSError as exc:
                self.fail(exc)
        return snapshot

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"notices": len(self.notices), "state_path": str(self._state_path or "")})
        return info
