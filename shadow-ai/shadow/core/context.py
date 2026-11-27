"""The shared context every engine receives.

It owns the pieces an engine must not build for itself: configuration, the
event bus, the journal, the launcher client, and the one browser session.
`ensure_cdp()` is the only place that starts a profile, so the Supervisor has
a single call to wrap and the Recovery Engine a single call to retry.
"""

from __future__ import annotations

import logging
import random
import threading
from typing import Any, Dict, Optional

from ..errors import ConfigError
from ..transport.cdp import CdpClient
from ..transport.domprobe import PageProbe
from ..transport.shardx import ShardxApi
from .config import ShadowConfig
from .events import EventBus
from .journal import Journal
from .registry import Registry


class ShadowContext:
    def __init__(self, cfg: Optional[ShadowConfig] = None, bus: Optional[EventBus] = None,
                 journal: Optional[Journal] = None, api: Optional[ShardxApi] = None,
                 rng: Optional[random.Random] = None, mock_launcher: Any = None):
        self.cfg = cfg or ShadowConfig.load()
        self.bus = bus or EventBus()
        self.journal = journal or Journal(self.cfg.journal_path(), sync=False)
        self.registry = Registry(bus=self.bus)
        self.rng = rng or random.Random()
        self.log = logging.getLogger("shadow")
        self.profile_id = self.cfg.default_profile
        self._cdp: Optional[CdpClient] = None
        self._probe: Optional[PageProbe] = None
        self._api = api
        self._mock = mock_launcher
        self._lock = threading.RLock()
        self.cancelled = threading.Event()
        self.stats: Dict[str, Any] = {}

    # -- launcher -----------------------------------------------------
    @property
    def api(self) -> ShardxApi:
        if self._api is None:
            token = "" if self.cfg.mock else self.cfg.resolve_token()
            self._api = ShardxApi(self.cfg.api_base, token=token)
        return self._api

    def set_api(self, api: ShardxApi) -> None:
        self._api = api

    # -- browser ------------------------------------------------------
    @property
    def cdp(self) -> Optional[CdpClient]:
        return self._cdp

    def ensure_cdp(self, profile_id: str = "", reuse: bool = True) -> CdpClient:
        """Return a connected CDP client, starting the profile if it is down."""
        with self._lock:
            pid = profile_id or self.profile_id
            if not pid and self.cfg.mock:
                pid = "win-rtx4060"   # the mock ships with profiles; pick the first
            if not pid:
                raise ConfigError("no profile chosen: set default_profile or pass --profile")
            if reuse and self._cdp is not None and self._alive(self._cdp):
                return self._cdp
            self.close_cdp()
            if self.cfg.cdp_http_url:
                http_url = self.cfg.cdp_http_url
            elif self.cfg.mock:
                http_url = self._mock_http_url(pid)
            else:
                self.api.require_token()
                http_url = self.api.cdp_http_url(pid, autostart=True, headless=self.cfg.headless)
            client = CdpClient.from_http(http_url, timeout=30)
            client.connect()
            client.attach_page()
            # A build without the patched domain still works — it just loses the
            # human input path, and every engine that types or clicks says so.
            motion = client.motion_available()
            if motion:
                try:
                    client.pointer_create(0, 0, self.cfg.human.pace, self.cfg.human.pointer_seed)
                except Exception:
                    motion = False
            self._cdp = client
            self._probe = PageProbe(client)
            self.profile_id = pid
            self.bus.publish("browser.attached", source="context", profile=pid,
                             cdp_http_url=http_url, motion=motion)
            if not motion:
                self.bus.publish("browser.degraded", source="context", profile=pid,
                                 reason="this engine has no Motion domain; input falls back to Input.*")
            return client

    @staticmethod
    def _alive(client: CdpClient) -> bool:
        try:
            return not client._closed and bool(client.current_url() is not None)
        except Exception:
            return False

    @property
    def probe(self) -> PageProbe:
        if self._probe is None:
            raise ConfigError("no browser attached; call ensure_cdp() first")
        return self._probe

    def page(self, profile_id: str = "") -> PageProbe:
        self.ensure_cdp(profile_id)
        assert self._probe is not None
        return self._probe

    def close_cdp(self) -> None:
        client, self._cdp = self._cdp, None
        self._probe = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # -- mock ---------------------------------------------------------
    def _mock_http_url(self, profile_id: str) -> str:
        """Endpoint for a mock profile, opening one engine per context.

        The mock launcher is per context, not a process-wide singleton: two
        runs must not share one browser, and a test that wants a build
        without the Motion domain must get the one it built.
        """
        from ..mock import MockLauncher
        if self._mock is None:
            self._mock = MockLauncher(token="shadow-mock", human_speed=0.0).start()
            self.bus.publish("mock.started", source="context", port=self._mock.port)
        pid = profile_id if profile_id in self._mock.profiles else list(self._mock.profiles)[0]
        for server in [self._mock.cdp_servers.get(pid)]:
            if server is not None and getattr(server, "session", None) is not None \
                    and not server.session.closed:
                return f"http://127.0.0.1:{server.server_address[1]}"
        port = self._mock._open_engine(self._mock.profiles[pid], True)
        self._mock.profiles[pid].running = True
        self._mock.profiles[pid].cdp_port = port
        return f"http://127.0.0.1:{port}"

    # -- misc ---------------------------------------------------------
    def cancel(self) -> None:
        self.cancelled.set()
        self.bus.publish("task.cancelled", source="context", profile=self.profile_id)

    def reset_cancel(self) -> None:
        self.cancelled.clear()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "profile": self.profile_id,
            "mock": bool(self.cfg.mock),
            "api_base": self.cfg.api_base,
            "issued_token": bool(self.cfg.api_token or self.cfg.launcher_settings),
            "attached": self._cdp is not None,
            "degraded": bool(self._cdp and self._cdp.degraded),
            "engines": {r.name: r.state for r in self.registry.records()},
        }

    def close(self) -> None:
        self.close_cdp()
        if self._mock is not None:
            try:
                self._mock.stop()
            except Exception:
                pass
        try:
            self.journal.close()
        except Exception:
            pass
