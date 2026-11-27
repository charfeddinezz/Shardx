"""Small helpers shared by every engine: time, ids, retry, redaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import random
import re
import string
import time
import uuid
from typing import Any, Callable, Iterable, Optional


def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}{raw}" if prefix else raw


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def jdump(obj: Any, **kw) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, **kw)


def jload(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------
# crypto-ish, stdlib only (used to mint the launcher's HS256 bearer token)
# --------------------------------------------------------------------------

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hs256_jwt(secret: str, payload: dict) -> str:
    header = b64url(jdump({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(jdump(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{b64url(sig)}"


def verify_hs256(secret: str, token: str) -> bool:
    try:
        header, body, sig = token.split(".")
    except ValueError:
        return False
    expected = b64url(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return hmac.compare_digest(expected, sig)


# --------------------------------------------------------------------------
# retry / backoff
# --------------------------------------------------------------------------

class Backoff:
    """Exponential backoff with full jitter, capped.

    Used by the Recovery Engine and the Supervisor. Kept deterministic when a
    `rng` is supplied so tests can pin the sequence.
    """

    def __init__(self, base: float = 0.5, cap: float = 30.0, factor: float = 2.0,
                 jitter: bool = True, rng: Optional[random.Random] = None):
        self.base = max(0.01, base)
        self.cap = max(self.base, cap)
        self.factor = max(1.0, factor)
        self.jitter = jitter
        self._rng = rng or random.Random()

    def delay(self, attempt: int) -> float:
        raw = min(self.cap, self.base * (self.factor ** max(0, attempt - 1)))
        if not self.jitter:
            return raw
        return self._rng.uniform(raw / 2.0, raw)


def retry(times: int = 3, backoff: Optional[Backoff] = None, on: tuple = (Exception,),
          should_retry: Optional[Callable[[Exception], bool]] = None,
          sleep: Callable[[float], None] = time.sleep):
    """Decorator for synchronous calls. Async callers use `retry_async`."""

    def wrap(fn):
        def inner(*args, **kwargs):
            bo = backoff or Backoff()
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except on as exc:  # noqa: PERF203
                    last = exc
                    if should_retry and not should_retry(exc):
                        raise
                    if attempt == times:
                        raise
                    sleep(bo.delay(attempt))
            raise last  # pragma: no cover
        inner.__name__ = getattr(fn, "__name__", "wrapped")
        return inner
    return wrap


def retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False))


# --------------------------------------------------------------------------
# text + redaction
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+[\w\-\.=]+|password|passwd|secret|token|api[_-]?key)\b\s*[:=]?\s*(\S+)")
_CARD_RE = re.compile(r"\b\d{12,19}\b")


def redact(text: str) -> str:
    """Strip credentials from anything that goes to a log or a dashboard."""
    if not text:
        return text
    out = _TOKEN_RE.sub(lambda m: f"{m.group(1)} <redacted>", text)
    return _CARD_RE.sub("<redacted-number>", out)


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if re.search(r"(?i)pass(word)?|secret|token|authorization|cookie|api[_-]?key", str(k)):
                clean[k] = "<redacted>"
            else:
                clean[k] = redact_obj(v)
        return clean
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"\s+", "-", (text or "").strip().lower())
    s = re.sub(r"[^a-z0-9\u0600-\u06ff\-_]+", "", s)
    return (s[:limit].strip("-") or "task")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def truncate(text: str, limit: int = 240) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def entropy_id(n: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def chunked(seq: Iterable, size: int):
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default
