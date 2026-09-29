"""MCP server: tools listed, no send tool exists, drafts don't send."""
from __future__ import annotations


def _cfg():
    from mailbot.config import Config

    c = Config()
    c.agent.send_mode = "auto"
    c.agent.daily_token_cap = 10_000_000
    return c


def test_tool_names_have_no_send():
    from mailbot.mcp_server import build_server

    server = build_server(_cfg())

    async def names():
        tools = await server.list_tools()
        return sorted(t.name for t in tools)

    import asyncio

    got = asyncio.run(names())
    assert "search_mail" in got
    assert "read_message" in got
    assert "read_thread" in got
    assert "calendar" in got
    assert "dossier" in got
    assert "create_draft" in got
    assert not any("send" in n for n in got), got


def test_search_reads_without_sending(provider, cfg, monkeypatch):
    import mailbot.mcp_server as M

    provider.add_message(sender="a@b.com", subject="Hello",
                         body="Just saying hello to you friend")
    monkeypatch.setattr(M, "_providers", lambda cfg: {"google": provider})
    server = M.build_server(cfg)
    fn = server._tool_manager._tools["search_mail"].fn
    out = fn(query="hello", sender="", limit=10, account="")
    assert out["count"] == 1
    assert out["messages"][0]["subject"] == "Hello"
    assert provider.sent == []


def test_create_draft_does_not_send(provider, cfg, monkeypatch):
    import mailbot.mcp_server as M

    monkeypatch.setattr(M, "_providers", lambda cfg: {"google": provider})
    server = M.build_server(cfg)
    fn = server._tool_manager._tools["create_draft"].fn
    out = fn(to=["a@b.com"], subject="Hi", body="hello friend", account="")
    assert out["ok"] is True
    assert provider.sent == []
    assert len(provider.drafted) == 1
