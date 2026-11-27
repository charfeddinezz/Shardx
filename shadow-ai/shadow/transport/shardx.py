"""Client for the ShardX launcher's local automation API (`127.0.0.1:40325`).

Only the endpoints SHADOW actually uses are wrapped, and every wrapper keeps
the launcher's own vocabulary: profiles, sessions, proxies, fingerprints,
folders, tags, templates, trash. Nothing here invents a "browser" or a "tab"
where the product calls it a profile.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..errors import ApiError, ConfigError
from .http import HttpClient


class ShardxApi:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0):
        self.http = HttpClient(base_url, token=token, timeout=timeout)
        self.base_url = self.http.base_url

    # -- health / identity -------------------------------------------
    def health(self) -> Dict[str, Any]:
        return self.http.json_get("/health", timeout=5)

    def running(self) -> List[Dict[str, Any]]:
        return self.http.json_get("/running") or []

    def is_up(self) -> bool:
        try:
            self.health()
            return True
        except Exception:
            return False

    # -- profiles -----------------------------------------------------
    def profiles(self) -> List[Dict[str, Any]]:
        data = self.http.json_get("/profiles")
        return data if isinstance(data, list) else (data or {}).get("profiles", [])

    def profile(self, profile_id: str) -> Dict[str, Any]:
        return self.http.json_get(f"/profiles/{profile_id}")

    def create_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.http.json_post("/profiles", payload)

    def temporary_profile(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.http.json_post("/profiles/temporary", payload or {})

    def delete_profile(self, profile_id: str) -> Any:
        return self.http.delete(f"/profiles/{profile_id}").json({})

    # -- sessions -----------------------------------------------------
    def start(self, profile_id: str, headless: bool = False, password: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"headless": bool(headless)}
        if password:
            body["password"] = password
        data = self.http.json_post(f"/profiles/{profile_id}/start?cdp=true", body, timeout=120) or {}
        data.setdefault("profile_id", profile_id)
        return data

    def stop(self, profile_id: str) -> Dict[str, Any]:
        return self.http.json_post(f"/profiles/{profile_id}/stop", {}, timeout=30) or {}

    def cdp_for(self, profile_id: str, autostart: bool = True,
                headless: bool = False, wait_s: float = 60.0) -> Dict[str, Any]:
        """Return the `cdp` block of a running profile, starting it if needed."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for entry in self.running():
                if entry.get("profile_id") == profile_id and entry.get("cdp"):
                    return entry["cdp"]
            if not autostart:
                break
            started = self.start(profile_id, headless=headless)
            cdp = started.get("cdp")
            if cdp:
                return cdp
            if started.get("cdp_error"):
                raise ApiError(500, f"profile started without CDP: {started['cdp_error']}")
            time.sleep(1.0)
        raise ApiError(408, f"profile {profile_id!r} did not expose a CDP endpoint within {wait_s:.0f}s")

    def cdp_http_url(self, profile_id: str, autostart: bool = True, headless: bool = False) -> str:
        cdp = self.cdp_for(profile_id, autostart=autostart, headless=headless)
        url = cdp.get("http_url") or cdp.get("web_socket_debugger_url")
        if not url:
            raise ApiError(500, "the launcher returned a CDP block with no address")
        if url.startswith("ws://") and "http_url" in cdp:
            url = cdp["http_url"]
        return url

    # -- assets -------------------------------------------------------
    def proxies(self) -> List[Dict[str, Any]]:
        data = self.http.json_get("/proxies")
        return data if isinstance(data, list) else []

    def add_proxy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.http.json_post("/proxies", payload)

    def fingerprints(self) -> List[Dict[str, Any]]:
        data = self.http.json_get("/fingerprints")
        return data if isinstance(data, list) else []

    def new_fingerprint(self, platform: str = "") -> Dict[str, Any]:
        path = f"/fingerprint/new/{platform}" if platform else "/fingerprint/new"
        return self.http.json_get(path).get("fingerprint", {}) if isinstance(self.http.json_get(path), dict) else {}

    def folders(self) -> List[Dict[str, Any]]:
        data = self.http.json_get("/folders")
        return data if isinstance(data, list) else []

    def cookies(self, profile_id: str) -> Any:
        return self.http.json_get(f"/profiles/{profile_id}/cookies")

    def import_cookies(self, profile_id: str, cookies: List[Dict[str, Any]]) -> Any:
        return self.http.json_post(f"/profiles/{profile_id}/cookies", {"cookies": cookies})

    # -- automation projects -----------------------------------------
    def projects(self) -> List[Dict[str, Any]]:
        data = self.http.json_get("/automation/projects")
        return data if isinstance(data, list) else []

    def run_project(self, project_id: str, profile_ids: Optional[List[str]] = None) -> Any:
        return self.http.json_post(f"/automation/projects/{project_id}/run",
                                   {"profile_ids": profile_ids or []}, timeout=60)

    def run_status(self, project_id: str) -> Any:
        return self.http.json_get(f"/automation/projects/{project_id}/status")

    # -- guard --------------------------------------------------------
    def require_token(self) -> None:
        if not self.http.token:
            raise ConfigError(
                "no token: set SHADOW_API_TOKEN, or SHADOW_LAUNCHER_SETTINGS to the "
                "launcher's settings.json so SHADOW can mint one. Copy a token from "
                "ShardX > Settings > Automation API."
            )
