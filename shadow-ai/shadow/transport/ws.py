"""A small RFC 6455 client, enough for the Chrome DevTools Protocol.

pip-free on purpose: the launcher is a single desktop install and SHADOW
must run on a machine with nothing but Python. Frames are sent as binary
(CDP accepts text or binary; Chrome answers what it was sent).
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Optional

from ..errors import TransportError

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsClient:
    def __init__(self, url: str, timeout: float = 30.0, origin: Optional[str] = None,
                 max_message: int = 64 * 1024 * 1024):
        self.url = url
        self.timeout = timeout
        self.origin = origin
        self.max_message = max_message
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    # -- lifecycle ----------------------------------------------------
    def connect(self) -> "WsClient":
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise TransportError(f"not a websocket url: {self.url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            raw = socket.create_connection((host, port), timeout=self.timeout)
            raw.settimeout(self.timeout)
            if parts.scheme == "wss":
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=host)
            self._sock = raw
            self._handshake(host, port, path)
        except TransportError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise TransportError(f"cannot reach {self.url}: {exc}") from None
        return self

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.origin:
            lines.append(f"Origin: {self.origin}")
        if any(s in self.url for s in ("/devtools/browser",)):
            pass  # Chrome rejects a restrictive Origin on some builds; none is safest
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())  # type: ignore[union-attr]
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(4096)  # type: ignore[union-attr]
            if not chunk:
                raise TransportError("websocket handshake closed early")
            data += chunk
            if len(data) > 65536:
                raise TransportError("websocket handshake response too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101 " not in status_line:
            raise TransportError(f"websocket handshake refused: {status_line.strip()}")
        self._buffer = rest

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, b"\x03\xe8")
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    def __enter__(self) -> "WsClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- framing ------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise TransportError("websocket is not connected")
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            if self._sock is None:
                raise TransportError("websocket closed")
            chunk = self._sock.recv(max(4096, n - len(self._buffer)))
            if not chunk:
                raise TransportError("websocket closed by peer")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def _recv_frame(self) -> tuple[int, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > self.max_message:
            raise TransportError(f"websocket message too large ({length} bytes)")
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length) if length else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, opcode, data

    # -- message API --------------------------------------------------
    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    def recv_text(self) -> str:
        """Read one whole message, honoring FIN and answering pings."""
        parts = []
        fragments = 0
        while True:
            fin, opcode, data = self._recv_frame()
            if opcode == OP_CLOSE:
                raise TransportError("websocket closed by peer")
            if opcode == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            parts.append(data)
            fragments += 1
            if fragments > 10_000:
                raise TransportError("websocket message has too many fragments")
            if fin:
                return b"".join(parts).decode("utf-8", "replace")


class WsServer(WsClient):
    """A websocket already accepted by an HTTP upgrade (server side).

    Used by the mock engine so tests exercise the same framing code the
    product uses in the client role.
    """

    def __init__(self, sock, timeout: float = 60.0):
        super().__init__("ws://server/local", timeout=timeout)
        self._sock = sock
        self._buffer = b""

    def connect(self) -> "WsServer":
        return self
