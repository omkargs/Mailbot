"""Single inbox, deliberately.

This module used to be multi-inbox profiles: named bindings with their own
OAuth, voice and model overrides. That was cut — a second inbox doubled the
authority surface (whose allowlist? whose voice? which identity sends?) and
produced eight real bugs for a feature nobody asked for.

What remains is the seam, not the feature. Call sites still import these
names, and they now describe one inbox:

* `active(cfg, providers)` yields `(None, provider)` per provider — the
  scan loop is unchanged, the profile is always None.
* `model_for(profile, default)` is the global model. Always.
* `header_for(profile, address)` is empty. One inbox needs no tag.
* `get_current()` is None. There is nothing to switch between.
* `/profiles` and `/change-profile` answer "single inbox" instead of
  listing anything. `/whoami` still works.

The profiles TABLE stays in the schema untouched: dropping a table from a
shipped SQLite database breaks every existing install on upgrade, and an
empty table costs nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import CONFIG_PATH


def ensure_migrated() -> bool:
    return False


def list_profiles() -> list[dict[str, Any]]:
    return []


def get_current() -> dict[str, Any] | None:
    return None


def current_profile() -> dict[str, Any] | None:
    return None


def set_current(profile_id: str) -> dict[str, Any] | None:
    return None


def active(cfg, providers: dict[str, Any]) -> list[tuple[None, Any]]:
    """Every provider, each running profile-less on the global model."""
    return [(None, p) for _, p in providers.items()]


def model_for(profile: dict[str, Any] | None, default: str) -> str:
    return default


def header_for(profile: dict[str, Any] | None, address: str = "") -> str:
    return ""


def slug(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "inbox"


def creds_file(profile_id: str) -> Path:
    from .config import CONFIG_DIR

    return CONFIG_DIR / f"google-credentials-{profile_id}.json"


def token_file(profile_id: str) -> Path:
    from .config import CONFIG_DIR

    return CONFIG_DIR / f"google-token-{profile_id}.json"


def add_profile(name: str, account: str, model_override: str = "") -> dict[str, Any]:
    raise ValueError("multi-inbox profiles were removed — single inbox only")


def config_path() -> Path:
    return CONFIG_PATH
