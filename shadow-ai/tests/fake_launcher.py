"""A stand-in for the ShardX launcher's automation API.

It is not a guess: the routes, the auth rule and the JSON shapes are copied
from this repository's own `src-tauri/src/api.rs` and `openapi.yaml`
(`GET /health` open, everything else behind a Bearer token, `POST
/profiles/{id}/start` returning the `cdp` block, `GET /profiles` carrying the
`cdp` of the running profiles). Behind it sits a real CDP server, so a test
using this proves the whole live path — token → profile → CDP → page — and not
just that a mock answered.

What it cannot prove is Chrome itself; that needs a machine with the launcher
installed, which is what `shadow doctor --live` is for.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class FakeLauncher:
    def __init__(self, token: str = "live-token", cdp_url: str = "",
                 profiles: Optional[List[str]] = None, fail_start: str = ""):
        self.token = token
        self.cdp_url = cdp_url
        self.profiles = {pid: {"id": pid, "name": pid.title(), "running": False,
                               "pid": 0, "cdp": None, "folder": ""}
                         for pid in (profiles or ["main-profile", "second-profile"])}
        self.fail_start = fail_start
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # -- plumbing ------------------------------------------------
            def _reply(self, code: int, payload: Any) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return {}

            def _authorized(self) -> bool:
                if not outer.token:
                    return True
                header = self.headers.get("Authorization") or ""
                return header == f"Bearer {outer.token}"

            def log_message(self, *args):
                pass

            # -- routes --------------------------------------------------
            def do_GET(self):                          # noqa: N802
                path = self.path.split("?")[0]
                outer.requests.append({"method": "GET", "path": path})
                if path == "/health":
                    return self._reply(200, {"ok": True, "name": "shardx-launcher",
                                             "version": "2.1.6"})
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                if path == "/profiles":
                    listed = []
                    for profile in outer.profiles.values():
                        entry = dict(profile)
                        entry["cdp"] = profile["cdp"] if profile["running"] else None
                        listed.append(entry)
                    return self._reply(200, listed)
                if path == "/running":
                    return self._reply(200, [
                        {"profile_id": p["id"], "pid": p["pid"], "cdp": p["cdp"]}
                        for p in outer.profiles.values() if p["running"]])
                return self._reply(404, {"error": "not found"})

            def do_POST(self):                         # noqa: N802
                path = self.path.split("?")[0]
                body = self._body()
                outer.requests.append({"method": "POST", "path": path, "body": body})
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                parts = [p for p in path.split("/") if p]
                if len(parts) == 3 and parts[0] == "profiles" and parts[2] in ("start", "stop"):
                    profile = outer.profiles.get(parts[1])
                    if profile is None:
                        return self._reply(404, {"error": "no such profile"})
                    if parts[2] == "stop":
                        profile["running"] = False
                        profile["cdp"] = None
                        return self._reply(200, {"profile_id": profile["id"]})
                    if outer.fail_start and outer.fail_start == profile["id"]:
                        return self._reply(500, {"error": "the engine did not start"})
                    profile["running"] = True
                    profile["pid"] = 4242
                    profile["cdp"] = {"port": 0, "http_url": outer.cdp_url,
                                      "web_socket_debugger_url": ""}
                    return self._reply(200, {"profile_id": profile["id"], "pid": 4242,
                                             "headless": bool(body.get("headless")),
                                             "cdp": profile["cdp"]})
                return self._reply(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
