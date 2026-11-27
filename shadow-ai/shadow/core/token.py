"""Mint the bearer token the launcher's local API expects.

The launcher stores an HS256 signing secret in its `settings.json` and signs
ten-year tokens with claims {sub, iat, exp} (see `src-tauri/src/api.rs`).
Reading that file from the same machine lets SHADOW start with no pasted
token at all. Nothing leaves the computer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from ..errors import ConfigError
from ..util import hs256_jwt

TEN_YEARS = 60 * 60 * 24 * 365 * 10
SUBJECT = "shardx-api"


def candidate_settings_paths() -> list[Path]:
    home = Path.home()
    out = [
        home / ".config" / "shardx-launcher" / "settings.json",          # Linux
        home / "Library" / "Application Support" / "shardx-launcher" / "settings.json",  # macOS
    ]
    appdata = home / "AppData" / "Roaming" / "shardx-launcher" / "settings.json"  # Windows
    out.append(appdata)
    return out


def read_secret(path: Optional[str] = None) -> str:
    candidates = [Path(path).expanduser()] if path else candidate_settings_paths()
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read {candidate}: {exc}") from exc
        text = _decode(raw)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"{candidate} is not valid JSON: {exc}") from exc
        secret = (data or {}).get("api_secret") or ""
        if secret:
            return secret
        raise ConfigError(f"{candidate} has no api_secret yet; open ShardX once, "
                          "or paste a token from Settings > Automation API")
    raise ConfigError("no ShardX settings.json found; set SHADOW_API_TOKEN instead, "
                      "or SHADOW_LAUNCHER_SETTINGS to the file's path")


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    if len(raw) > 1 and raw[0] == 0 and raw[1] != 0:
        return raw.decode("utf-16-be")
    return raw.decode("utf-8", "replace")


def mint_from_settings_file(path: str) -> str:
    secret = read_secret(path)
    now = int(time.time())
    return hs256_jwt(secret, {"sub": SUBJECT, "iat": now, "exp": now + TEN_YEARS})


def mint_from_secret(secret: str, ttl_s: int = TEN_YEARS) -> str:
    now = int(time.time())
    return hs256_jwt(secret, {"sub": SUBJECT, "iat": now, "exp": now + ttl_s})
