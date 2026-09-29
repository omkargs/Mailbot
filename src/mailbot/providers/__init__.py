"""Provider registry. Builds configured providers; the agent sees one interface."""
from __future__ import annotations

import logging

from .. import config
from .base import DraftRequest, EventRequest, MailProvider

log = logging.getLogger(__name__)

__all__ = ["MailProvider", "DraftRequest", "EventRequest", "build_providers"]


def build_providers(cfg: config.Config | None = None) -> dict[str, MailProvider]:
    """Return {account_id: provider} for every enabled, configured account."""
    cfg = cfg or config.load()
    out: dict[str, MailProvider] = {}
    enabled = cfg.agent.enabled_accounts

    want_google = not enabled or "google" in enabled
    if want_google and cfg.google.credentials_file:
        from .gmail import GmailProvider

        out["google"] = GmailProvider(
            credentials_file=cfg.google.credentials_file,
            token_file=cfg.google.token_file,
            address=cfg.google.account,
            display_name=cfg.google.display_name,
            auto_send=cfg.google.auto_send,
            calendar_enabled=cfg.google.calendar_enabled,
        )

    # Extra Gmail inboxes, one per profile (account id "google:<slug>").
    # Each carries its own OAuth files, voice, and model override, so family
    # mail never trains the company voice or vice versa.
    try:
        from .. import profiles as _profiles

        for prof in _profiles.list_profiles():
            acct = prof.get("account", "")
            if not acct.startswith("google:") or acct in out:
                continue
            if enabled and "google" not in enabled and acct not in enabled:
                continue
            creds = _profiles.creds_file(prof["id"])
            if not creds.exists():
                continue
            from .gmail import GmailProvider as _GP

            # The address lives in the accounts table, set at sign-in time.
            # Without it every chat header and every "who is this" reads
            # back blank or as the raw account id.
            addr = ""
            try:
                from ..storage import db as _db

                _db.ensure_migrated()
                with _db.db() as _c:
                    _r = _c.execute("SELECT address FROM accounts WHERE id=?",
                                    (acct,)).fetchone()
                addr = (_r["address"] if _r else "") or ""
            except Exception as _e:
                log.debug("address lookup failed for %s: %s", acct, type(_e).__name__)

            out[acct] = _GP(
                credentials_file=str(creds),
                token_file=str(_profiles.token_file(prof["id"])),
                address=addr,
                display_name=prof.get("name", ""),
                auto_send=cfg.google.auto_send,
                calendar_enabled=cfg.google.calendar_enabled,
                account=acct,
            )
    except Exception as e:
        log.warning("extra inbox profiles skipped: %s", type(e).__name__)

    want_ms = "microsoft" in enabled or (enabled and "ms" in enabled)
    if want_ms and cfg.microsoft.client_id and cfg.microsoft.tenant_id:
        from .graph import GraphProvider

        out["microsoft"] = GraphProvider(
            tenant_id=cfg.microsoft.tenant_id,
            client_id=cfg.microsoft.client_id,
            client_secret=cfg.microsoft.client_secret,
            address=cfg.microsoft.account,
            display_name=cfg.microsoft.display_name,
            auto_send=cfg.microsoft.auto_send,
            calendar_enabled=cfg.microsoft.calendar_enabled,
            token_file=cfg.microsoft.token_file,
        )

    return out
