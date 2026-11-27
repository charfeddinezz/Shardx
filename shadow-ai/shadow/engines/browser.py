"""Browser Engine — the profile lifecycle, in one place the Supervisor can restart.

Holds the CDP attachment. `health()` is a real round trip (it reads the URL),
not a flag, so a browser that died under the process is reported dead.
"""

from __future__ import annotations

from typing import Any, Dict

from ..errors import EngineDown
from .base import Engine


class BrowserEngine(Engine):
    name = "browser"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.attached_once = False
        self.restarts = 0

    def attach(self, profile_id: str = "") -> Any:
        cdp = self.ctx.ensure_cdp(profile_id or self.ctx.profile_id)
        self.attached_once = True
        return cdp

    def navigate(self, url: str) -> Dict[str, Any]:
        cdp = self.attach()
        cdp.navigate(url)
        return {"url": cdp.current_url(), "title": cdp.title()}

    def url(self) -> str:
        cdp = self.ctx.cdp
        if cdp is None:
            return ""
        try:
            return cdp.current_url()
        except Exception:
            return ""

    def stop_profile(self) -> Dict[str, Any]:
        pid = self.ctx.profile_id
        self.ctx.close_cdp()
        if self.ctx.cfg.mock or not pid:
            return {"stopped": True, "profile": pid}
        return self.ctx.api.stop(pid)

    def restart(self) -> None:
        self.restarts += 1
        self.ctx.close_cdp()
        self.attach()

    def health(self) -> Dict[str, Any]:
        info = super().health()
        alive = False
        try:
            if self.ctx.cdp is not None:
                self.ctx.cdp.current_url()
                alive = True
        except Exception as exc:  # noqa: BLE001
            info["last_error"] = f"{type(exc).__name__}: {exc}"
        info.update({"attached": self.ctx.cdp is not None, "alive": alive,
                     "profile": self.ctx.profile_id, "restarts": self.restarts,
                     "degraded": bool(self.ctx.cdp and self.ctx.cdp.degraded)})
        info["healthy"] = bool(info.get("healthy")) and (alive or not self.attached_once)
        if self.attached_once and not alive:
            info["healthy"] = False
        return info

    def require(self) -> Any:
        if self.ctx.cdp is None:
            raise EngineDown("no browser attached")
        return self.ctx.cdp
