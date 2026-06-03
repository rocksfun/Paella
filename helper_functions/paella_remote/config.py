"""Load remote server settings from TOML-style config files."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List

from helper_functions.SYSTEM_pull_config_io import parse_toml_config

_WIN_OVERRIDE = "C:/Paella local/remote_server_config.txt"
_BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "references",
    "remote_server_config.txt",
)


def _resolve_config_path() -> str:
    if os.path.exists(_WIN_OVERRIDE):
        return _WIN_OVERRIDE
    if hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "references", "remote_server_config.txt")
        if os.path.exists(bundled):
            return bundled
    return _BUNDLED


@dataclass(frozen=True)
class RemoteServerConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8765
    api_key: str = "change-me-before-lab-use"
    status_interval_hz: float = 2.0
    frequency_max_hz: float = 10.0
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    discovery_enabled: bool = True
    discovery_port: int = 9876
    discovery_secret: str = ""


def load_remote_server_config() -> RemoteServerConfig:
    path = _resolve_config_path()
    with open(path, "r", encoding="utf-8") as f:
        parsed = parse_toml_config(f.read())
    section = parsed.get("remote_server", {})

    enabled = section.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "yes")

    cors_raw = section.get("cors_origins", "*")
    if isinstance(cors_raw, str):
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
    elif isinstance(cors_raw, list):
        origins = [str(o) for o in cors_raw]
    else:
        origins = ["*"]

    disc_enabled = section.get("discovery_enabled", True)
    if isinstance(disc_enabled, str):
        disc_enabled = disc_enabled.lower() in ("true", "1", "yes")

    return RemoteServerConfig(
        enabled=bool(enabled),
        host=str(section.get("host", "0.0.0.0")),
        port=int(section.get("port", 8765)),
        api_key=str(section.get("api_key", "change-me-before-lab-use")),
        status_interval_hz=float(section.get("status_interval_hz", 2)),
        frequency_max_hz=float(section.get("frequency_max_hz", 10)),
        cors_origins=origins or ["*"],
        discovery_enabled=bool(disc_enabled),
        discovery_port=int(section.get("discovery_port", 9876)),
        discovery_secret=str(section.get("discovery_secret", "")),
    )
