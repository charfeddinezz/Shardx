"""Memory Engine — what SHADOW remembers between runs.

Three kinds of memory, all on this computer:

* **facts** — namespaced key/value (`site:shop.local`, `task:abc`, `global`).
* **checkpoints** — a task's step index plus the state it needs to resume.
* **lessons** — selectors and flows that worked, so the next run on the same
  site starts from what worked instead of rediscovering it.

Writes are atomic (temp file + replace) and the file is small JSON: a memory
you cannot open and read is not yours.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..util import iso, now
from .base import Engine


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = {"version": 1, "facts": {}, "checkpoints": {}, "lessons": {}, "meta": {}}
        self.load()

    # -- persistence --------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return
            if isinstance(raw, dict):
                for key in ("facts", "checkpoints", "lessons", "meta"):
                    if isinstance(raw.get(key), dict):
                        self.data[key] = raw[key]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    # -- facts --------------------------------------------------------
    def set(self, key: str, value: Any, namespace: str = "global") -> None:
        with self._lock:
            bucket = self.data["facts"].setdefault(namespace, {})
            bucket[key] = {"value": value, "ts": iso()}
            self.save()

    def get(self, key: str, namespace: str = "global", default: Any = None) -> Any:
        with self._lock:
            entry = (self.data["facts"].get(namespace) or {}).get(key)
            return entry["value"] if isinstance(entry, dict) and "value" in entry else default

    def keys(self, namespace: str = "global") -> List[str]:
        return sorted((self.data["facts"].get(namespace) or {}).keys())

    def namespaces(self) -> List[str]:
        return sorted(self.data["facts"].keys())

    def forget(self, key: str, namespace: str = "global") -> bool:
        with self._lock:
            bucket = self.data["facts"].get(namespace) or {}
            if key in bucket:
                del bucket[key]
                self.save()
                return True
            return False

    # -- checkpoints --------------------------------------------------
    def checkpoint(self, task_id: str, step: int, state: Dict[str, Any]) -> None:
        with self._lock:
            self.data["checkpoints"][task_id] = {"step": step, "state": state, "ts": iso()}
            self.save()

    def resume_point(self, task_id: str) -> Optional[Dict[str, Any]]:
        entry = self.data["checkpoints"].get(task_id)
        return dict(entry) if isinstance(entry, dict) else None

    def clear_checkpoint(self, task_id: str) -> None:
        with self._lock:
            if self.data["checkpoints"].pop(task_id, None) is not None:
                self.save()

    # -- lessons ------------------------------------------------------
    def lesson(self, site: str, what: str, detail: Any) -> None:
        with self._lock:
            bucket = self.data["lessons"].setdefault(site, {})
            bucket[what] = {"detail": detail, "ts": iso(), "hits": int((bucket.get(what) or {}).get("hits", 0)) + 1}
            self.save()

    def lessons(self, site: str) -> Dict[str, Any]:
        return dict(self.data["lessons"].get(site) or {})


class MemoryEngine(Engine):
    name = "memory"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.store = MemoryStore(ctx.cfg.memory_path())
        self.writes = 0

    def remember(self, key: str, value: Any, namespace: str = "global") -> None:
        self.store.set(key, value, namespace)
        self.writes += 1
        self.note(key=key, namespace=namespace)

    def recall(self, key: str, namespace: str = "global", default: Any = None) -> Any:
        return self.store.get(key, namespace, default)

    def checkpoint(self, task_id: str, step: int, state: Dict[str, Any]) -> None:
        self.store.checkpoint(task_id, step, state)
        self.note(task=task_id, step=step)

    def resume_point(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.store.resume_point(task_id)

    def learn(self, url: str, what: str, detail: Any) -> None:
        site = (url or "").split("//")[-1].split("/")[0] or "unknown"
        self.store.lesson(site, what, detail)

    def lessons_for(self, url: str) -> Dict[str, Any]:
        site = (url or "").split("//")[-1].split("/")[0] or "unknown"
        return self.store.lessons(site)

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"writes": self.writes, "namespaces": len(self.store.namespaces()),
                     "checkpoints": len(self.store.data["checkpoints"]),
                     "path": str(self.store.path)})
        return info
