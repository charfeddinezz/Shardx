"""Append-only journal — the memory the Supervisor uses to resume.

One JSON object per line, flushed and fsynced as it is written, so a machine
that dies mid-task still has every step up to the last one. The journal is
also the audit trail: nothing is redacted out of it except credentials.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..util import iso, new_id, now, redact_obj


@dataclass
class JournalEntry:
    kind: str
    task_id: str = ""
    step: int = -1
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=now)
    id: str = field(default_factory=lambda: new_id("j-"))

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "ts": iso(self.ts), "kind": self.kind, "task_id": self.task_id,
                "step": self.step, "data": redact_obj(self.data)}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "JournalEntry":
        return cls(kind=raw.get("kind", "?"), task_id=raw.get("task_id", ""),
                   step=int(raw.get("step", -1) or -1), data=raw.get("data") or {},
                   id=raw.get("id") or new_id("j-"))


class Journal:
    def __init__(self, path: Path, sync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sync = sync
        self.scrub = None           # set by the runtime: SafetyEngine.scrub_output
        self._handle = open(self.path, "a", encoding="utf-8")

    # -- writing ------------------------------------------------------
    def append(self, kind: str, task_id: str = "", step: int = -1, **data: Any) -> JournalEntry:
        entry = JournalEntry(kind=kind, task_id=task_id, step=step, data=data)
        scrubber = self.scrub
        if scrubber is not None:
            # Values typed into secret fields are removed by value, not only by
            # key, before the line ever reaches the disk.
            entry.data = scrubber(entry.data)
        line = json.dumps(entry.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            if self._sync:
                os.fsync(self._handle.fileno())
        return entry

    # -- reading ------------------------------------------------------
    def entries(self, task_id: str = "", kinds: Optional[Iterable[str]] = None) -> List[JournalEntry]:
        wanted = set(kinds) if kinds else None
        out: List[JournalEntry] = []
        with self._lock:
            if not self.path.exists():
                return out
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue  # a torn last line is expected after a crash
                    if task_id and raw.get("task_id") != task_id:
                        continue
                    if wanted and raw.get("kind") not in wanted:
                        continue
                    out.append(JournalEntry.from_dict(raw))
        return out

    def last_step(self, task_id: str) -> int:
        last = -1
        for entry in self.entries(task_id=task_id, kinds={"step.done", "step.failed", "step.started"}):
            last = max(last, entry.step)
        return last

    def task_ids(self) -> List[str]:
        seen: List[str] = []
        for entry in self.entries():
            if entry.task_id and entry.task_id != "-" and entry.task_id not in seen:
                seen.append(entry.task_id)
        return seen

    def compact(self, keep_last: int = 5000) -> int:
        """Rewrite the file keeping the tail. Returns lines dropped."""
        with self._lock:
            if not self.path.exists():
                return 0
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= keep_last:
                return 0
            keep = lines[-keep_last:]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            self._handle.close()
            self._handle = open(self.path, "a", encoding="utf-8")
            return len(lines) - len(keep)

    def close(self) -> None:
        with self._lock:
            try:
                self._handle.close()
            except Exception:
                pass

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
