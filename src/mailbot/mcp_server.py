"""MCP server mode: your mailbox as tools for any MCP client.

`mail-agent mcp` speaks MCP over stdio. Point Claude Desktop (or any
MCP client) at it and the assistant can search mail, read threads,
check the calendar, and prepare drafts — inside YOUR session, under
YOUR roof.

Authority, same as everywhere:

- Reads are free: search, fetch, threads, calendar, dossier.
- NOTHING sends. There is no send tool here at all — no approval flow
  exists over MCP in v1, so sending stays inside Mailbot's own loop.
- Drafts are allowed (reversible, you review them in Gmail).
- Scheduling is allowed (creates a job you can cancel with `tasks`).

Claude Desktop config snippet:

    {"mcpServers": {"mailbot": {
      "command": "/home/YOU/Mailbot/.venv/bin/mail-agent",
      "args": ["mcp"]
    }}}
"""
from __future__ import annotations

from typing import Any


def _providers(cfg):
    from .providers import build_providers

    out = {}
    for name, p in build_providers(cfg).items():
        if p.valid():
            out[name] = p
    return out


def _first(cfg, account: str = ""):
    provs = _providers(cfg)
    if account and account in provs:
        return provs[account]
    if not provs:
        raise RuntimeError("no authenticated mailbox — run `mail-agent setup --step google`")
    return next(iter(provs.values()))


def build_server(cfg):
    from mcp.server.fastmcp import FastMCP

    from .dossier import build_dossier

    mcp = FastMCP("mailbot")

    @mcp.tool()
    def search_mail(query: str = "", sender: str = "", limit: int = 10,
                    account: str = "") -> dict[str, Any]:
        """Search the mailbox (Gmail syntax). Read-only."""
        p = _first(cfg, account)
        rows = p.search(query=query, sender=sender, limit=min(limit, 25),
                        full=False)
        return {"account": p.account, "count": len(rows), "messages": [
            {"id": r.get("id"), "from": r.get("sender"),
             "subject": r.get("subject"), "date": r.get("date"),
             "snippet": r.get("snippet", "")[:200]} for r in rows]}

    @mcp.tool()
    def read_message(message_id: str, account: str = "") -> dict[str, Any]:
        """Fetch one full message body. Read-only."""
        p = _first(cfg, account)
        m = p.get_message(message_id)
        if not m:
            return {"ok": False, "error": "not found"}
        m.pop("label_ids", None)
        return {"ok": True, "message": m}

    @mcp.tool()
    def read_thread(thread_id: str, account: str = "") -> dict[str, Any]:
        """Whole conversation, oldest first. Read-only."""
        p = _first(cfg, account)
        return {"ok": True, "messages": p.get_thread(thread_id)}

    @mcp.tool()
    def calendar(limit: int = 10, account: str = "") -> dict[str, Any]:
        """Upcoming events. Read-only."""
        p = _first(cfg, account)
        if not p.calendar_enabled:
            return {"ok": False, "error": "calendar disabled"}
        return {"ok": True, "events": p.list_events(limit=min(limit, 25))}

    @mcp.tool()
    def dossier(account: str = "") -> dict[str, Any]:
        """Persona mined from the mailbox. Read-only."""
        p = _first(cfg, account)
        return build_dossier(p.account, cfg)

    @mcp.tool()
    def create_draft(to: list[str], subject: str, body: str,
                     account: str = "") -> dict[str, Any]:
        """Prepare a draft (reversible — you review it in Gmail). Never sends."""
        from .providers.base import DraftRequest

        p = _first(cfg, account)
        did = p.create_draft(DraftRequest(to=to, subject=subject, body=body))
        return {"ok": bool(did), "draft_id": did}

    return mcp


def cmd_mcp(args, cfg) -> int:
    build_server(cfg).run(transport="stdio")
    return 0
