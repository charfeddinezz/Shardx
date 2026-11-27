"""HTTP client for the launcher's local API. Standard library only."""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from ..errors import ApiError, AuthError, TransportError
from ..util import Backoff, redact

DEFAULT_TIMEOUT = 30.0


class HttpResponse:
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]):
        self.status = status
        self.body = body
        self.headers = dict(headers)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self, default: Any = None) -> Any:
        if not self.body:
            return default
        try:
            return json.loads(self.body)
        except ValueError:
            return default


class HttpClient:
    """A very small JSON HTTP client with retry and token handling."""

    def __init__(self, base_url: str, token: str = "", timeout: float = DEFAULT_TIMEOUT,
                 retries: int = 2, backoff: Optional[Backoff] = None,
                 opener: Optional[Any] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff or Backoff(base=0.4, cap=4.0)
        self._opener = opener or urllib.request.build_opener()
        self.calls = 0

    # -- public -------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None,
                query: Optional[Mapping[str, Any]] = None,
                timeout: Optional[float] = None) -> HttpResponse:
        url = self.base_url + path
        if query:
            clean = {k: str(v).lower() if isinstance(v, bool) else v
                     for k, v in query.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last: Optional[BaseException] = None
        for attempt in range(1, self.retries + 2):
            self.calls += 1
            try:
                return self._once(method, url, payload, headers, timeout or self.timeout)
            except (AuthError, ApiError):
                raise
            except TransportError as exc:
                last = exc
                if attempt > self.retries:
                    break
                time.sleep(self.backoff.delay(attempt))
        raise TransportError(f"{method} {path} failed: {last}")

    def get(self, path: str, **kw) -> HttpResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> HttpResponse:
        return self.request("POST", path, body=body, **kw)

    def delete(self, path: str, **kw) -> HttpResponse:
        return self.request("DELETE", path, **kw)

    def json_get(self, path: str, **kw) -> Any:
        return self.get(path, **kw).json({})

    def json_post(self, path: str, body: Any = None, **kw) -> Any:
        return self.post(path, body, **kw).json({})

    # -- internals ----------------------------------------------------
    def _once(self, method: str, url: str, payload: Optional[bytes],
              headers: Dict[str, str], timeout: float) -> HttpResponse:
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as res:
                return HttpResponse(res.status, res.read(), res.headers)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            if exc.code in (401, 403):
                raise AuthError(
                    "the launcher rejected the token (401/403). Paste a fresh token from "
                    "Settings > Automation API, or point SHADOW_LAUNCHER_SETTINGS at settings.json",
                    status=exc.code,
                ) from None
            message = _error_message(body) or f"HTTP {exc.code}"
            raise ApiError(exc.code, message, method, url) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"{method} {_short(url)} unreachable: {redact(str(reason))}") from None

    def close(self) -> None:
        try:
            close = getattr(self._opener, "close", None)
            if close:
                close()
        except Exception:
            pass


def _error_message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body.decode("utf-8", "replace")[:300]
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)[:300]
    return str(data)[:300]


def _short(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return parts.path or "/"
