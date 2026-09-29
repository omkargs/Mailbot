"""Profiles: named inbox bindings. Step A — model + migration only.

A profile = one account's mail + its own voice + an optional model
override. Behaviour changes ride on top in later steps (per-profile scan,
chat switching); this module only stores profiles and migrates the old
single-account install to exactly one profile.

config.json shape (new key, everything else untouched):

    "profiles": [
      {"id": "personal", "name": "Personal", "account": "google",
       "model_override": ""}
    ]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import CONFIG_PATH


def _read_cfg() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _write_cfg(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))
    CONFIG_PATH.chmod(0o600)


def list_profiles() -> list[dict[str, Any]]:
    """DB rows are the source of truth; config.json mirrors them."""
    from .storage import db

    db.migrate()
    with db.db() as c:
        rows = c.execute("SELECT * FROM profiles ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def get_current() -> dict[str, Any] | None:
    from .storage import db

    db.migrate()
    with db.db() as c:
        row = c.execute(
            "SELECT * FROM profiles WHERE is_current=1 LIMIT 1").fetchone()
    return dict(row) if row else None


def set_current(profile_id: str) -> dict[str, Any] | None:
    """Switch the active profile. Exactly one current at any time.
    Returns the new current row, or None when the id is unknown."""
    from .storage import db

    db.migrate()
    with db.db() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=?",
                        (profile_id,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE profiles SET is_current=0")
        c.execute("UPDATE profiles SET is_current=1 WHERE id=?",
                  (profile_id,))
        return dict(c.execute("SELECT * FROM profiles WHERE id=?",
                              (profile_id,)).fetchone())


def ensure_migrated() -> bool:
    """Migrate a single-account install to one 'personal' profile.

    Idempotent: returns True only when it actually migrated something.
    Never touches an install that already has profiles.
    """
    from .storage import db

    db.migrate()
    with db.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM profiles").fetchone()["n"]
        if n:
            return False
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        c.execute(
            "INSERT INTO profiles (id, name, account, model_override, "
            "is_current, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("personal", "Personal", "google", "", 1, now),
        )
    data = _read_cfg()
    if "profiles" not in data:
        data["profiles"] = [
            {"id": "personal", "name": "Personal", "account": "google",
             "model_override": ""}
        ]
        _write_cfg(data)
    return True


def active(cfg, providers: dict[str, Any]) -> list[tuple[dict[str, Any] | None, Any]]:
    """Profiles bound to live providers, in order.

    Runs ensure_migrated so a legacy install yields exactly one profile.
    Providers with no profile row (shouldn't happen) fall back to None,
    which means legacy display and the global model.
    """
    ensure_migrated()
    rows = {r["account"]: r for r in list_profiles()}
    return [(rows.get(name), p) for name, p in providers.items()]


def model_for(profile: dict[str, Any] | None, default: str) -> str:
    """Profile model override, falling back to the first/global model."""
    if profile and (profile.get("model_override") or "").strip():
        return profile["model_override"].strip()
    return default


def header_for(profile: dict[str, Any] | None, address: str = "") -> str:
    """Chat header: [profilename · inbox]. Empty profile = legacy display."""
    if not profile:
        return f"[{address}]" if address else ""
    name = profile.get("name", profile.get("id", "?"))
    acct = profile.get("account", "")
    who = f"{name} · {address or acct}"
    return f"[{who}]"


def config_path() -> Path:
    return CONFIG_PATH
