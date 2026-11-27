"""Safety Engine — the last thing every action passes through.

The rule is simple: nothing is refused silently and nothing is approved
silently. `check()` returns a verdict for an action; a destructive or
irreversible action without an explicit approval raises `ApprovalRequired`,
and the caller has to ask a person. The engine never grants that approval by
itself, and it never blocks a read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from ..errors import ApprovalRequired, SafetyViolation
from .base import Engine

SENSITIVE_LABEL = re.compile(
    r"(?i)(password|passwd|pwd|card|cvv|cvc|iban|routing|ssn|social security|passport|"
    r"mot de passe|كلمة المرور|بطاقة|رمز)")
PAYMENT_TEXT = re.compile(r"(?i)\b(pay\s*now|place\s*order|complete\s*(purchase|order)|buy\s*now|"
                          r"confirm\s*payment|ادفع|أكد الطلب|payer maintenant)\b")
DESTRUCTIVE_TEXT = re.compile(r"(?i)\b(delete|remove|erase|cancel\s+(order|account)|unsubscribe|"
                              r"احذف|إلغاء|supprimer)\b")


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    needs_approval: bool = False
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "needs_approval": self.needs_approval, "category": self.category}


class SafetyEngine(Engine):
    name = "safety"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.checked = 0
        self.blocked = 0
        self.approvals = 0

    # -- URL ----------------------------------------------------------
    def check_url(self, url: str) -> Verdict:
        self.checked += 1
        cfg = self.ctx.cfg.safety
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https"):
            return self._block("url", f"scheme {parts.scheme or '(none)'} is not http(s)", url=url)
        if cfg.https_only and parts.scheme != "https":
            return self._block("url", "https_only is on and this URL is plain http", url=url)
        if any(host == blocked or host.endswith("." + blocked) for blocked in cfg.blocked_hosts):
            return self._block("url", f"{host} is on the blocked list (local and metadata hosts)", url=url)
        if ":" in parts.netloc and not host:
            return self._block("url", "unparseable host", url=url)
        return Verdict(True, category="url")

    # -- actions ------------------------------------------------------
    def check_action(self, action: str, target: str = "", value: str = "",
                     url: str = "", approved: bool = False) -> Verdict:
        """Classify an action. Raises on refusal; returns on acceptance."""
        self.checked += 1
        cfg = self.ctx.cfg.safety
        label = f"{target} {value}".strip()

        if action in ("click", "submit") and PAYMENT_TEXT.search(label or ""):
            self.approvals += 1
            if not approved:
                raise ApprovalRequired(
                    f"{action} {label!r} looks like a payment or order confirmation",
                    category="payment", action=action, target=target)
        if action in ("type", "fill", "login") and cfg.redact_sensitive_inputs and SENSITIVE_LABEL.search(label or ""):
            # Typing a password is legitimate; recording it is not.
            self.emit("sensitive_input", action=action, target=target[:60])
        if action == "click" and DESTRUCTIVE_TEXT.search(label or ""):
            self.approvals += 1
            if not approved:
                raise ApprovalRequired(f"{action} {label!r} may be destructive",
                                       category="destructive", action=action, target=target)
        if action == "upload" and not approved:
            self.approvals += 1
            raise ApprovalRequired("uploads need an explicit yes", category="upload")
        if url:
            verdict = self.check_url(url)
            if not verdict.allowed:
                raise SafetyViolation(verdict.reason, category=verdict.category, url=url)
        return Verdict(True, category=action)

    def check_os_input(self) -> Verdict:
        if not self.ctx.cfg.safety.allow_os_input:
            return self._block("os_input",
                               "real OS mouse/keyboard control is off (safety.allow_os_input)")
        return Verdict(True, category="os_input")

    def redact(self, value: str) -> str:
        """What may go into memory or a log for a sensitive field."""
        if not self.ctx.cfg.safety.redact_sensitive_inputs:
            return value
        return "<redacted>" if value else ""

    # -- housekeeping -------------------------------------------------
    def _block(self, category: str, reason: str, **details) -> Verdict:
        self.blocked += 1
        self.emit("blocked", category=category, reason=reason, **details)
        return Verdict(False, reason=reason, category=category)

    def enforce(self, verdict: Verdict) -> None:
        if not verdict.allowed:
            raise SafetyViolation(verdict.reason, category=verdict.category)

    def budget(self, task) -> Optional[str]:
        """Return why a task must stop, or None while it is inside its budget."""
        cfg = self.ctx.cfg.safety
        if task is None:
            return None
        if task.steps_done > cfg.max_steps_per_task:
            return f"step budget of {cfg.max_steps_per_task} exhausted"
        import time
        if time.time() - (task.started_at or time.time()) > cfg.max_task_seconds:
            return f"time budget of {cfg.max_task_seconds}s exhausted"
        return None

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"checked": self.checked, "blocked": self.blocked,
                     "approvals_requested": self.approvals,
                     "os_input": self.ctx.cfg.safety.allow_os_input,
                     "system_commands": self.ctx.cfg.safety.allow_system_commands})
        return info
