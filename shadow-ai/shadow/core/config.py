"""Configuration.

Precedence, lowest to highest: built-in defaults, `shadow.json` next to the
package (or `$SHADOW_CONFIG`), environment variables, explicit overrides.
The token is never printed by `describe()` and can be read from the
launcher's own settings file when you do not want to paste it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import ConfigError

ENV_PREFIX = "SHADOW_"


@dataclass
class SafetyConfig:
    # Anything in this set needs an explicit yes before it happens.
    confirm_actions: List[str] = field(default_factory=lambda: [
        "purchase", "payment", "transfer", "delete_account", "send_message",
        "post_public", "change_password", "download_file", "upload_file",
    ])
    # Hosts we will not touch at all, whatever the task says.
    blocked_hosts: List[str] = field(default_factory=lambda: [
        "localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254",
        "metadata.google.internal",
    ])
    # Real OS-level mouse and keyboard. Off by default, always.
    allow_os_input: bool = False
    # Real shell commands. Off by default, allowlisted when on.
    allow_system_commands: bool = False
    allowed_commands: List[str] = field(default_factory=lambda: ["echo", "date", "hostname"])
    # Typing into fields that look like passwords / card numbers / OTPs.
    redact_sensitive_inputs: bool = True
    # A ceiling on how long one task may run before the supervisor steps in.
    max_task_seconds: int = 900
    max_steps_per_task: int = 500
    # Refuse to act on a page whose URL is not http(s).
    https_only: bool = False


@dataclass
class HumanConfig:
    """Playback of a human-ish operator. All speeds are multipliers."""
    pace: float = 1.0
    typo_rate: float = 0.0
    read_delay_ms: int = 180
    think_pause_ms: int = 400
    pointer_seed: Optional[int] = None


@dataclass
class VisionConfig:
    ocr: str = "auto"            # auto | tesseract | none
    tesseract_lang: str = "eng"
    max_image_bytes: int = 6_000_000
    diff_threshold: float = 0.02  # share of changed pixels that counts as "changed"
    prefer_dom: bool = True       # read the DOM before asking a model about pixels


@dataclass
class IntelligenceConfig:
    provider: str = "heuristic"    # heuristic | ollama | openai_compatible
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.1"
    timeout_s: float = 60.0
    max_tokens: int = 700
    temperature: float = 0.2
    # The planner refuses to let the model choose anything outside this set.
    allow_actions: List[str] = field(default_factory=lambda: [
        "goto", "click", "type", "press", "scroll", "wait", "extract", "assert",
        "back", "forward", "reload", "hover", "select", "submit", "remember",
        "branch", "finish",
    ])


@dataclass
class SupervisorConfig:
    interval_s: float = 5.0
    max_restarts: int = 3
    restart_window_s: float = 300.0
    heartbeat_timeout_s: float = 45.0
    resume_tasks: bool = True
    quarantine_after: int = 5


@dataclass
class ShadowConfig:
    api_base: str = "http://127.0.0.1:40325"
    api_token: str = ""
    # When set, the token is minted locally from the launcher's api_secret
    # instead of being pasted. Handy on the machine that runs ShardX.
    launcher_settings: str = ""
    default_profile: str = ""
    cdp_http_url: str = ""        # attach to a profile already running
    mock: bool = False
    log_level: str = "INFO"
    state_dir: str = ""           # default: ~/.shadow-ai
    headless: bool = False
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    human: HumanConfig = field(default_factory=HumanConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    intelligence: IntelligenceConfig = field(default_factory=IntelligenceConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)

    # -- paths --------------------------------------------------------
    def state_path(self) -> Path:
        base = Path(self.state_dir).expanduser() if self.state_dir else Path.home() / ".shadow-ai"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def journal_path(self) -> Path:
        return self.state_path() / "journal.jsonl"

    def memory_path(self) -> Path:
        return self.state_path() / "memory.json"

    def data_path(self) -> Path:
        return self.state_path() / "data"

    # -- resolution ---------------------------------------------------
    def resolve_token(self) -> str:
        """Return the bearer token, minting one from the launcher secret if asked."""
        if self.api_token:
            return self.api_token
        if self.launcher_settings:
            from .token import mint_from_settings_file
            return mint_from_settings_file(self.launcher_settings)
        return ""

    def secret_bits(self) -> Dict[str, Any]:
        d = asdict(self)
        for key in ("api_token", "launcher_settings"):
            if d.get(key):
                d[key] = "<set>"
        return d

    def describe(self) -> str:
        return json.dumps(self.secret_bits(), indent=2, ensure_ascii=False)

    # -- loading ------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> "ShadowConfig":
        cfg = cls()
        candidate = Path(path or os.environ.get(f"{ENV_PREFIX}CONFIG") or (Path(__file__).resolve().parent.parent.parent / "shadow.json"))
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as exc:
                raise ConfigError(f"cannot read {candidate}: {exc}") from exc
            cfg = cls.from_dict(data, base=cfg)
        cfg = cfg._apply_env(os.environ)
        if overrides:
            cfg = cls.from_dict(overrides, base=cfg)
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base: Optional["ShadowConfig"] = None) -> "ShadowConfig":
        cfg = base or cls()
        nested = {"safety": SafetyConfig, "human": HumanConfig, "vision": VisionConfig,
                  "intelligence": IntelligenceConfig, "supervisor": SupervisorConfig}
        for key, value in (data or {}).items():
            if key in nested:
                current = getattr(cfg, key)
                merged = asdict(current)
                merged.update(value or {})
                setattr(cfg, key, _build(nested[key], merged))
            elif key in {f.name for f in fields(cls)}:
                setattr(cfg, key, value)
            else:
                raise ConfigError(f"unknown setting {key!r}")
        return cfg

    def _apply_env(self, env: Dict[str, str]) -> "ShadowConfig":
        mapping = {
            "API_BASE": ("root", "api_base", str),
            "API_TOKEN": ("root", "api_token", str),
            "LAUNCHER_SETTINGS": ("root", "launcher_settings", str),
            "DEFAULT_PROFILE": ("root", "default_profile", str),
            "CDP_HTTP_URL": ("root", "cdp_http_url", str),
            "MOCK": ("root", "mock", _as_bool),
            "LOG_LEVEL": ("root", "log_level", str),
            "STATE_DIR": ("root", "state_dir", str),
            "HEADLESS": ("root", "headless", _as_bool),
            "PROVIDER": ("intelligence", "provider", str),
            "MODEL": ("intelligence", "model", str),
            "BASE_URL": ("intelligence", "base_url", str),
            "ALLOW_OS_INPUT": ("safety", "allow_os_input", _as_bool),
            "ALLOW_SYSTEM_COMMANDS": ("safety", "allow_system_commands", _as_bool),
        }
        for suffix, (where, name, caster) in mapping.items():
            raw = env.get(ENV_PREFIX + suffix)
            if raw is None or raw == "":
                continue
            value = caster(raw)
            if where == "root":
                setattr(self, name, value)
            else:
                setattr(getattr(self, where), name, value)
        return self


def _build(cls, data: Dict[str, Any]):
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in allowed})


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
