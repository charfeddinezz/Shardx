"""A local stand-in for the ShardX launcher: HTTP API + a CDP websocket."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..transport.ws import WsServer
from ..util import new_id, now
from .engine import MockEngineSession


@dataclass
class MockProfile:
    id: str
    name: str = ""
    running: bool = False
    headless: bool = False
    proxy_id: Optional[str] = None
    folder: str = ""
    tags: List[str] = field(default_factory=list)
    cdp_port: int = 0
    pid: int = 0
    started_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name or self.id, "running": self.running,
                "proxy_id": self.proxy_id, "folder": self.folder, "tags": list(self.tags)}


class MockLauncher:
    """Runs on 127.0.0.1 with an ephemeral port; no network, no browser."""

    def __init__(self, token: str = "test-token", profiles: Optional[List[str]] = None,
                 human_speed: float = 0.0, seed: int = 7, motion: bool = True):
        self.token = token
        self.human_speed = human_speed
        self.motion = motion
        self.rng = random.Random(seed)
        self.profiles: Dict[str, MockProfile] = {}
        for pid in profiles or ["win-rtx4060", "mac-studio", "linux-box"]:
            self.profiles[pid] = MockProfile(id=pid, name=pid.replace("-", " ").title())
        self.sessions: List[ThreadingHTTPServer] = []
        self.engine_threads: List[threading.Thread] = []
        self.requests: List[dict] = []
        self._lock = threading.RLock()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.port = 0
        self.cdp_servers: Dict[str, threading.Thread] = {}

    # -- lifecycle ----------------------------------------------------
    def start(self) -> "MockLauncher":
        launcher = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _reply(self, status: int, payload: Any = None) -> None:
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                header = self.headers.get("Authorization", "")
                if header != f"Bearer {launcher.token}":
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                path = urlparse(self.path)
                launcher.requests.append({"method": "GET", "path": path.path})
                if path.path == "/health":
                    return self._reply(200, {"ok": True, "service": "shardx-mock"})
                if not self._auth():
                    return
                if path.path == "/profiles":
                    return self._reply(200, [p.to_dict() for p in launcher.profiles.values()])
                if path.path == "/running":
                    out = []
                    for p in launcher.profiles.values():
                        if p.running:
                            out.append({"profile_id": p.id, "pid": p.pid,
                                        "cdp": {"port": p.cdp_port,
                                                "http_url": f"http://127.0.0.1:{p.cdp_port}",
                                                "web_socket_debugger_url":
                                                    f"ws://127.0.0.1:{p.cdp_port}/devtools/browser/mock-{p.id}"}})
                    return self._reply(200, out)
                if path.path.startswith("/profiles/"):
                    pid = path.path.split("/")[2]
                    profile = launcher.profiles.get(pid)
                    if not profile:
                        return self._reply(404, {"error": f"no profile {pid}"})
                    return self._reply(200, profile.to_dict())
                if path.path == "/proxies":
                    return self._reply(200, [{"id": "proxy-dc1", "type": "socks5", "host": "10.0.0.1", "port": 1080}])
                if path.path == "/fingerprints":
                    return self._reply(200, [{"id": "fp-win-1", "platform": "windows"}])
                if path.path == "/folders":
                    return self._reply(200, [{"name": "Ads"}])
                return self._reply(404, {"error": f"no route {path.path}"})

            def do_POST(self):
                path = urlparse(self.path)
                launcher.requests.append({"method": "POST", "path": path.path})
                if not self._auth():
                    return
                body = self._body()
                if path.path.endswith("/start"):
                    pid = path.path.split("/")[2]
                    profile = launcher.profiles.get(pid)
                    if profile is None:
                        return self._reply(404, {"error": f"no profile {pid}"})
                    with launcher._lock:
                        if not profile.running:
                            port = launcher._open_engine(profile, bool(body.get("headless")))
                            profile.running = True
                            profile.cdp_port = port
                            profile.pid = 4000 + len(launcher.profiles)
                            profile.headless = bool(body.get("headless"))
                            profile.started_at = now()
                    return self._reply(200, {"profile_id": pid, "pid": profile.pid,
                                             "headless": profile.headless,
                                             "cdp": {"port": profile.cdp_port,
                                                     "http_url": f"http://127.0.0.1:{profile.cdp_port}",
                                                     "web_socket_debugger_url":
                                                         f"ws://127.0.0.1:{profile.cdp_port}/devtools/browser/mock-{pid}"}})
                if path.path.endswith("/stop"):
                    pid = path.path.split("/")[2]
                    profile = launcher.profiles.get(pid)
                    if not profile:
                        return self._reply(404, {"error": f"no profile {pid}"})
                    stopped = profile.running
                    profile.running = False
                    profile.cdp_port = 0
                    return self._reply(200, {"profile_id": pid, "stopped": stopped})
                if path.path == "/profiles":
                    pid = new_id("p-")
                    launcher.profiles[pid] = MockProfile(id=pid, name=body.get("name", pid),
                                                         proxy_id=body.get("proxy_id"))
                    return self._reply(200, launcher.profiles[pid].to_dict())
                return self._reply(404, {"error": f"no route {path.path}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="mock-api", daemon=True).start()
        return self

    def _open_engine(self, profile: MockProfile, headless: bool) -> int:
        """Start a CDP-over-websocket server for one profile.

        The HTTP upgrade and the CDP session share one handler class, so the
        session must be built with the server captured *now*: binding it to
        `self` inside the handler would resolve at request time to whichever
        profile was opened last.
        """
        engine = self

        class CdpHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            rng = engine.rng
            human_speed = engine.human_speed
            motion_enabled = engine.motion
            profile_id = profile.id

            def log_message(self, *args):
                pass

            def do_GET(self):
                port = self.server.server_address[1]
                if self.path.startswith("/json/"):
                    ws = f"ws://127.0.0.1:{port}/devtools/browser/mock-{self.profile_id}"
                    if self.path.startswith("/json/version"):
                        body = json.dumps({"Browser": "ShardBrowser/158.0.8127.36 (mock)",
                                           "Protocol-Version": "1.3", "webSocketDebuggerUrl": ws})
                    else:
                        body = json.dumps([{"type": "page", "title": "mock",
                                            "webSocketDebuggerUrl": ws}])
                    raw = body.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if "devtools/browser" in self.path and self.headers.get("Upgrade", "").lower() == "websocket":
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    self.send_response(101, "Switching Protocols")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", _accept_key(key))
                    self.end_headers()
                    self.wfile.flush()
                    try:
                        sock = WsServer(_SocketAdapter(self.connection))
                        session = MockEngineSession(sock, self.profile_id, self.rng,
                                                    human_speed=self.human_speed,
                                                    motion=self.motion_enabled)
                        self.server.session = session
                        session.run()
                    except Exception:
                        import traceback
                        traceback.print_exc()
                    self.close_connection = True
                    return
                self.send_response(404)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), CdpHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name=f"mock-cdp-{profile.id}", daemon=True).start()
        self.cdp_servers[profile.id] = server  # type: ignore[assignment]
        return server.server_address[1]

    def stop(self) -> None:
        for server in list(self.cdp_servers.values()):
            try:
                server.shutdown()
            except Exception:
                pass
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    def __enter__(self) -> "MockLauncher":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _accept_key(key: str) -> str:
    import base64
    import hashlib
    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


class _SocketAdapter:
    """Present a handler's socket through the pair `WsClient` expects.

    The mock engine reuses the real `WsClient` framing code, so the tests
    exercise the same wire implementation the product uses.
    """

    def __init__(self, conn):
        self._conn = conn

    def recv(self, n: int) -> bytes:
        return self._conn.recv(n)

    def sendall(self, data: bytes) -> None:
        self._conn.sendall(data)

    def settimeout(self, t):
        self._conn.settimeout(t)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
