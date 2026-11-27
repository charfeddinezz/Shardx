"""Error taxonomy.

Every error the engines raise carries a `code` and a `severity`, so the
Recovery Engine can decide what to do without matching on message text.
"""

from __future__ import annotations


class ShadowError(Exception):
    """Base of everything SHADOW raises."""

    code = "shadow_error"
    severity = "error"
    retryable = False

    def __init__(self, message: str = "", **details):
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "retryable": self.retryable,
            "message": self.message,
            "details": self.details,
        }


class ConfigError(ShadowError):
    code = "config"
    severity = "fatal"


class AuthError(ShadowError):
    """The launcher refused the bearer token."""

    code = "auth"


class ApiError(ShadowError):
    """The launcher's local API answered with a non-2xx status."""

    code = "api"

    def __init__(self, status: int, message: str, method: str = "", path: str = ""):
        super().__init__(message, status=status, method=method, path=path)
        self.status = status

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        return self.status >= 500 or self.status == 429


class TransportError(ShadowError):
    """Socket, WebSocket or CDP-level failure."""

    code = "transport"
    retryable = True


class CdpError(ShadowError):
    """The browser answered a CDP command with an error object."""

    code = "cdp"


class ElementNotFound(ShadowError):
    code = "element_not_found"
    retryable = True


class AmbiguousTarget(ShadowError):
    """A selector matched more than one element and we refuse to guess."""

    code = "ambiguous_target"


class SafetyViolation(ShadowError):
    """The Safety Engine refused the action."""

    code = "safety_violation"
    severity = "warning"


class ApprovalRequired(ShadowError):
    code = "approval_required"
    severity = "warning"


class EngineDown(ShadowError):
    code = "engine_down"
    retryable = True


class StepFailed(ShadowError):
    code = "step_failed"

    def __init__(self, message: str, step: int = -1, **details):
        super().__init__(message, **details)
        self.step = step


class TaskAborted(ShadowError):
    code = "task_aborted"
    severity = "warning"


class VisionUnavailable(ShadowError):
    code = "vision_unavailable"
    severity = "warning"


class StateError(ShadowError):
    code = "state"
