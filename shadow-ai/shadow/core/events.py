"""Event bus.

One bus per process. Engines publish; the Supervisor, the journal, the
dashboard and the recovery engine subscribe. Two guarantees matter:

* a subscriber that raises can never break the publisher;
* publishing is cheap and never blocks: subscribers are called inline but
  each handler is wrapped, and an optional ring buffer keeps recent events
  for the dashboard without unbounded growth.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from ..util import iso, now, redact_obj

log = logging.getLogger("shadow.events")


@dataclass
class Event:
    topic: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=now)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"topic": self.topic, "ts": iso(self.ts), "source": self.source,
                "data": redact_obj(self.data)}


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, keep: int = 500):
        self._lock = threading.RLock()
        self._subs: Dict[str, List[Handler]] = {}
        self._wildcard: List[Handler] = []
        self._ring: Deque[Event] = deque(maxlen=max(10, keep))
        self._seq = 0

    # -- subscription -------------------------------------------------
    def on(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to `topic`, or to everything with `*` or `a.*`."""
        with self._lock:
            if topic in ("*", ""):
                self._wildcard.append(handler)
            else:
                self._subs.setdefault(topic, []).append(handler)

        def off() -> None:
            with self._lock:
                try:
                    (self._wildcard if topic in ("*", "") else self._subs.get(topic, [])).remove(handler)
                except ValueError:
                    pass

        return off

    def _matching(self, topic: str) -> List[Handler]:
        with self._lock:
            direct = list(self._subs.get(topic, ()))
            prefix = topic.rsplit(".", 1)[0] + ".*" if "." in topic else ""
            prefixd = list(self._subs.get(prefix, ())) if prefix else []
            return direct + prefixd + list(self._wildcard)

    # -- publishing ---------------------------------------------------
    def publish(self, topic: str, /, source: str = "", **data: Any) -> Event:
        """Publish. `topic` is positional-only, so a payload may use that name.

        `source` stays a keyword for readability; a payload key called
        "source" is renamed to "emitter" rather than raising.
        """
        if "source" in data:
            data["emitter"] = data.pop("source")
        ev = Event(topic=topic, data=data, source=source)
        with self._lock:
            self._seq += 1
            self._ring.append(ev)
        for handler in self._matching(topic):
            try:
                handler(ev)
            except Exception:  # a subscriber must never break a publisher
                log.exception("event handler failed for %s", topic)
        return ev

    def emit(self, event: Event) -> None:
        self.publish(event.topic, event.source, **event.data)

    # -- introspection ------------------------------------------------
    def recent(self, limit: int = 50, topic_prefix: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._ring)
        if topic_prefix:
            items = [e for e in items if e.topic.startswith(topic_prefix)]
        return [e.to_dict() for e in items[-limit:]]

    @property
    def sequence(self) -> int:
        return self._seq

    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._subs)


_bus: Optional[EventBus] = None


def bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus(keep: int = 500) -> EventBus:
    global _bus
    _bus = EventBus(keep=keep)
    return _bus
