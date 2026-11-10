"""Configuration management for oikb.

Resolution order (highest priority first):
  1. CLI flags (--url, --token)
  2. Environment variables (OPEN_WEBUI_URL, OPEN_WEBUI_API_KEY)
  3. Config file (~/.config/oikb/config.yaml)
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("OIKB_CONFIG_DIR", Path.home() / ".config" / "oikb"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def source_status_keys(entries: list[dict]) -> list[str]:
    """Keep existing status keys unless repeated sources need named identities."""
    counts = Counter(entry.get("source") for entry in entries)
    keys = []
    for entry in entries:
        source = entry.get("source")
        key = entry.get("name") if counts[source] > 1 else source
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Sources require source strings; repeated sources also require distinct nonempty names")
        if key in keys:
            raise ValueError(f"Duplicate source status key: {key}. Choose distinct source names.")
        keys.append(key)
    return keys


def _load_config_file() -> dict:
    """Load config from disk. Returns empty dict if file doesn't exist."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_config_file(data: dict) -> None:
    """Write config to disk, creating parent directories as needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)


def resolve_url(cli_url: str | None = None) -> str:
    """Resolve the Open WebUI base URL.

    Priority: cli_url > OPEN_WEBUI_URL > config file > error.
    """
    url = cli_url or os.environ.get("OPEN_WEBUI_URL") or _load_config_file().get("url")
    if not url:
        raise ValueError(
            "No Open WebUI URL configured. Set via:\n"
            "  export OPEN_WEBUI_URL=<base_url>\n"
            "  oikb config set url <base_url>\n"
            "  --url <base_url>"
        )
    return url.rstrip("/")


def resolve_token(cli_token: str | None = None) -> str:
    """Resolve the API token.

    Priority: cli_token > OPEN_WEBUI_API_KEY > config file > error.
    """
    token = cli_token or os.environ.get("OPEN_WEBUI_API_KEY") or _load_config_file().get("token")
    if not token:
        raise ValueError(
            "No API key configured. Set via:\n"
            "  export OPEN_WEBUI_API_KEY=<api_key>\n"
            "  oikb config set token <api_key>\n"
            "  --token <api_key>"
        )
    return token


def set_config(key: str, value: str) -> None:
    """Set a key in the config file."""
    data = _load_config_file()
    data[key] = value
    _save_config_file(data)


def get_config(key: str | None = None) -> dict | str | None:
    """Get a key (or all keys) from the config file."""
    data = _load_config_file()
    if key:
        return data.get(key)
    return data
