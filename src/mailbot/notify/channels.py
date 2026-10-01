"""Chat platform notifiers.

Discord is the primary approval channel: it gets interactive buttons, so
approving a queued send is one tap from the phone. Slack and Telegram are
supported; Telegram approves via /approve <id> commands.

Every notifier is a no-op when its token is unset, so an unconfigured
platform never breaks a run.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

import requests

log = logging.getLogger(__name__)

_timeout = (10.0, 30.0)


TELEGRAM_MAX_CHARS = 1800


def _compact(text: str) -> str:
    """Keep chat output short enough to read on a phone.

    The model was returning whole pasted mail bodies — long threads, full
    quotes — because a chat window gives no visible reason not to. This is
    the hard ceiling: trim at a line boundary and say it was trimmed, so a
    cut-off message never reads as a complete one.
    """
    if len(text) <= TELEGRAM_MAX_CHARS:
        return text
    head = text[:TELEGRAM_MAX_CHARS]
    cut = head.rfind("\n")
    if cut > TELEGRAM_MAX_CHARS * 0.6:
        head = head[:cut]
    return head.rstrip() + "\n\n… trimmed. Ask for the detail you need."


def _err_detail(e: Exception) -> str:
    """Pull the server's explanation out of an HTTP error.

    A bare exception class is useless for diagnosis. Telegram puts the actual
    reason in the response body ("Bad Request: can't parse entities: ...").
    """
    body = getattr(getattr(e, "response", None), "text", "") or ""
    return f"{e} {body[:160]}".strip() if body else str(e)[:160]


def _md_to_html(text: str) -> str:
    """Convert the markdown we generate into Telegram's HTML subset.

    Escape first, then convert. The order matters: converting first and
    escaping after would destroy the tags we just inserted.
    """
    h = (text.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

    # ``` fenced blocks and **bold** / *italic*. Telegram supports only a
    # subset of markdown, so keep it to what it renders reliably.
    h = re.sub(r"```(\w*)\n?(.*?)```", r"<pre>\2</pre>", h, flags=re.S)
    h = re.sub(r"`([^`]+)`", r"<code>\1</code>", h)
    h = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", h)
    h = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", h)
    return h


class BaseNotifier:
    name = "none"

    def send(self, text: str, approval_id: str = "", **kw: Any) -> str | None:
        return None

    def poll_once(self) -> list[dict[str, Any]]:
        return []

    def interactive(self) -> bool:
        return False


class DiscordNotifier(BaseNotifier):
    name = "discord"
    API = "https://discord.com/api/v10"

    def __init__(self, token: str, user_id: str = ""):
        self.token = token
        self.user_id = user_id
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bot {token}"

    def send(self, text: str, approval_id: str = "", **kw: Any) -> str | None:
        payload: dict[str, Any] = {"content": text[:1900]}
        if approval_id:
            payload["components"] = [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "Send", "custom_id": f"approve:{approval_id}"},
                    {"type": 2, "style": 4, "label": "Discard", "custom_id": f"deny:{approval_id}"},
                ],
            }]
        target = kw.get("channel")
        url = f"{self.API}/channels/{target}/messages" if target else f"{self.API}/users/@me/messages"
        try:
            r = self._session.post(url, json=payload, timeout=_timeout)
            r.raise_for_status()
            return r.json().get("id")
        except Exception as e:
            log.warning("discord send failed: %s", type(e).__name__)
            return None

    def interactive(self) -> bool:
        """Discord is outbound-only.

        Button presses cannot be polled. Discord has no REST endpoint that
        lists recent interactions — the earlier code called
        GET /users/@me/interactions, which does not exist, so it returned []
        forever and the approval buttons looked wired but never fired.

        Receiving button clicks requires holding a Gateway (WebSocket)
        connection, which is a real client, not a poll. Until that exists,
        reporting False here is honest: the agent treats Discord as a
        send-only channel and the operator approves on Telegram.
        """
        return False


class TelegramNotifier(BaseNotifier):
    name = "telegram"
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str = "", long_poll: bool = True):
        self.token = token
        self.chat_id = chat_id
        # Highest update_id consumed. Without it Telegram re-delivers the same
        # batch on every poll — and without PERSISTING it, every restart
        # re-answers everything since the last poll. That restart-echo is
        # where most "the bot said it twice" comes from.
        self._offset: int | None = self._load_offset()
        # Long polling: Telegram holds the request open and answers the instant
        # a message arrives, instead of us hammering getUpdates and finding
        # nothing. This is what makes chat feel instant.
        self.long_poll = long_poll

    @staticmethod
    def _offset_file() -> str:
        from ..config import DATA_DIR

        return str(DATA_DIR / "telegram_offset")

    def _load_offset(self) -> int | None:
        try:
            with open(self._offset_file()) as f:
                return int(f.read().strip() or 0) or None
        except (OSError, ValueError):
            return None

    def _save_offset(self) -> None:
        if not self._offset:
            return
        try:
            from pathlib import Path as _P

            p = _P(self._offset_file())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(self._offset))
        except OSError:
            pass

    def send(self, text: str, approval_id: str = "", **kw: Any) -> str | None:
        # Telegram is a phone, not a terminal. Long replies from the model
        # were arriving as a wall of pasted mail. Cap it, and say so rather
        # than cutting off mid-sentence without explanation.
        text = _compact(text)
        markup = None
        if approval_id:
            # Tap, don't type: Send/Discard buttons ride on the card, and the
            # /approve lines stay as the fallback for clients without them.
            # callback_data has a 64-byte ceiling — `ap:<id>` fits easily.
            text += f"\n\n/approve {approval_id}\n/discard {approval_id}"
            markup = {"inline_keyboard": [[
                {"text": "✅ Send", "callback_data": f"ap:{approval_id}"},
                {"text": "🗑 Discard", "callback_data": f"deny:{approval_id}"},
            ]]}
        # The brief and chat commands write markdown, not HTML. Sending that
        # under parse_mode=HTML makes Telegram reject the whole message with
        # HTTP 400 the moment any content contains a bare "<" — an address, a
        # company name, "3 < 5". So escape the HTML first, then convert the
        # markdown to the inline tags Telegram actually understands.
        text = _md_to_html(text)
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text,
                                   "parse_mode": "HTML"}
        if markup:
            payload["reply_markup"] = markup
        try:
            r = requests.post(f"{self.API}/bot{self.token}/sendMessage",
                              json=payload, timeout=_timeout)
            r.raise_for_status()
            return str(r.json().get("result", {}).get("message_id", ""))
        except Exception as e:
            # Log the body's description, not just the exception class. A bare
            # "HTTPError" told us nothing about why every send was failing.
            log.warning("telegram send failed: %s: %s", type(e).__name__, _err_detail(e))

    def answer_callback(self, callback_id: str, text: str) -> None:
        """Acknowledge a button tap. Without this the button spins forever
        on the operator's screen, which reads as a dead bot."""
        try:
            requests.post(f"{self.API}/bot{self.token}/answerCallbackQuery",
                          json={"callback_query_id": callback_id,
                                "text": text[:190]}, timeout=_timeout)
        except Exception as e:
            log.debug("answerCallbackQuery failed: %s", type(e).__name__)

    def strip_buttons(self, chat_id: str, message_id: int) -> None:
        """Remove the keyboard after a decision. The atomic approval claim
        already makes double-taps safe; this makes them visibly final."""
        try:
            requests.post(f"{self.API}/bot{self.token}/editMessageReplyMarkup",
                          json={"chat_id": chat_id, "message_id": message_id,
                                "reply_markup": {"inline_keyboard": []}},
                          timeout=_timeout)
        except Exception as e:
            log.debug("strip buttons failed: %s", type(e).__name__)

    def poll_once(self) -> list[dict[str, Any]]:
        """Fetch unconsumed updates.

        The offset must advance past everything we have seen. getUpdates
        re-returns the same unacknowledged batch on every call, so without an
        offset a chat message is answered again on every poll — and on a fast
        chat loop that is a message every few seconds, forever.

        With long polling the HTTP timeout must exceed Telegram's own timeout
        parameter, or the socket is cut before Telegram answers.
        """
        long_poll_sec = 25
        params: dict[str, Any] = {"timeout": long_poll_sec if self.long_poll else 0}
        if self._offset:
            params["offset"] = self._offset
        read_timeout = (10.0, long_poll_sec + 15.0) if self.long_poll else _timeout
        try:
            r = requests.get(f"{self.API}/bot{self.token}/getUpdates",
                             params=params, timeout=read_timeout)
            r.raise_for_status()
        except Exception:
            return []
        result = r.json().get("result", [])
        if result:
            # Telegram confirms the batch by advancing past its last id.
            self._offset = result[-1]["update_id"] + 1
            self._save_offset()
        out = []
        for upd in result:
            # Button taps arrive as callback queries, not messages. The tapper
            # must be the owner — anyone else's tap is dropped like a
            # stranger's message, for the same reason.
            q = upd.get("callback_query") or {}
            if q:
                if self.chat_id and str((q.get("from") or {}).get("id", "")) != str(self.chat_id):
                    log.warning("telegram: dropped button tap from unknown user")
                    continue
                data = str(q.get("data") or "")
                qmsg = q.get("message") or {}
                qchat = qmsg.get("chat") or {}
                if self.chat_id and str(qchat.get("id", "")) != str(self.chat_id):
                    continue
                for cmd, act in (("ap:", "approve"), ("deny:", "deny")):
                    if data.startswith(cmd) and len(data) > len(cmd):
                        out.append({"action": act, "approval_id": data[len(cmd):],
                                    "callback_id": q.get("id", ""),
                                    "callback_chat": str(qchat.get("id", "") or self.chat_id),
                                    "callback_msg": qmsg.get("message_id", 0)})
                continue
            msg = upd.get("message") or {}
            # Never respond to our own messages, or a bot's.
            if (msg.get("from") or {}).get("is_bot"):
                continue
            # SECURITY: only the owner's chat may talk to the agent. Telegram
            # delivers updates from ANY chat that messages the bot — without
            # this check a stranger gets a conversational agent over the
            # victim's mailbox (reads are free) plus /approve attempts.
            chat = msg.get("chat") or {}
            if self.chat_id and str(chat.get("id", "")) != str(self.chat_id):
                log.warning("telegram: dropped update from unknown chat id=%s",
                            chat.get("id"))
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            handled = False
            for cmd, act in (("/approve", "approve"), ("/discard", "deny")):
                if text.startswith(cmd):
                    parts = text.split()
                    if len(parts) > 1:
                        out.append({"action": act, "approval_id": parts[1]})
                    handled = True
            if not handled:
                # The chat key scopes conversation memory. Today every update
                # comes from the owner chat (anything else was dropped above),
                # but the key is explicit so a second chat can never inherit
                # this one's pronouns.
                out.append({"action": "chat", "text": text, "update_id": upd["update_id"],
                            "chat": f"telegram:{chat.get('id', '')}" if chat.get("id") else "owner"})
        return out

    def interactive(self) -> bool:
        """Telegram is polled for /approve and /discard, so it IS interactive.
        Without this the ApprovalListener never runs and every queued send
        would sit unanswered.
        """
        return True


class SlackNotifier(BaseNotifier):
    name = "slack"

    def __init__(self, token: str, channel: str = ""):
        self.token = token
        self.channel = channel

    def send(self, text: str, approval_id: str = "", **kw: Any) -> str | None:
        blocks = None
        if approval_id:
            blocks = [{
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Send"},
                     "action_id": f"approve:{approval_id}", "value": "approve"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Discard"},
                     "action_id": f"deny:{approval_id}", "value": "deny",
                     "style": "danger"},
                ],
            }]
        try:
            r = requests.post("https://slack.com/api/chat.postMessage",
                              headers={"Authorization": f"Bearer {self.token}"},
                              json={"channel": self.channel, "text": text[:3000], "blocks": blocks},
                              timeout=_timeout)
            r.raise_for_status()
            return r.json().get("ts")
        except Exception as e:
            log.warning("slack send failed: %s", type(e).__name__)
            return None


class MultiNotifier(BaseNotifier):
    """Fans out to every configured platform and collects approvals."""

    name = "multi"

    def __init__(self, notifiers: list[BaseNotifier]):
        self.notifiers = notifiers

    def send(self, text: str, approval_id: str = "", **kw: Any) -> str | None:
        first = None
        for n in self.notifiers:
            mid = n.send(text, approval_id=approval_id, **kw)
            first = first or mid
        return first

    def answer_callback(self, callback_id: str, text: str) -> None:
        for n in self.notifiers:
            fn = getattr(n, "answer_callback", None)
            if callable(fn):
                try:
                    fn(callback_id, text)
                except Exception:
                    pass

    def strip_buttons(self, chat_id: str, message_id: int) -> None:
        for n in self.notifiers:
            fn = getattr(n, "strip_buttons", None)
            if callable(fn):
                try:
                    fn(chat_id, message_id)
                except Exception:
                    pass

    def poll_once(self) -> list[dict[str, Any]]:
        out = []
        for n in self.notifiers:
            out += n.poll_once()
        return out

    def interactive(self) -> bool:
        return any(n.interactive() for n in self.notifiers)


def build_notifiers(cfg) -> BaseNotifier:
    n = cfg.notify
    out: list[BaseNotifier] = []
    if n.discord_token:
        out.append(DiscordNotifier(n.discord_token, n.discord_user_id))
    if n.telegram_token and n.telegram_chat_id:
        out.append(TelegramNotifier(n.telegram_token, n.telegram_chat_id))
    if n.slack_token and n.slack_channel:
        out.append(SlackNotifier(n.slack_token, n.slack_channel))
    if not out:
        return BaseNotifier()
    return MultiNotifier(out) if len(out) > 1 else out[0]


class ApprovalListener:
    """Polls the chat platform and executes decisions against the provider."""

    def __init__(self, cfg, providers: dict[str, Any]):
        self.cfg = cfg
        self.providers = providers
        self.notifier = build_notifiers(cfg)
        # Bounded dedupe: duplicates arrive seconds apart (restarts, racing
        # pollers), so a few thousand entries cover it. An unbounded set
        # grew for the lifetime of the daemon.
        from collections import deque

        self._seen: deque = deque(maxlen=5000)
        self._seen_set: set[str] = set()
        self._lock = threading.Lock()

    def _mark(self, key) -> bool:
        """True if already seen (skip it), else record and return False."""
        with self._lock:
            if key in self._seen_set:
                return True
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(key)
            self._seen_set.add(key)
            return False

    def tick(self) -> int:
        if not self.notifier.interactive():
            return 0
        from ..agent.chat import handle_text
        from ..agent.chatops import build_chat_ops
        from ..agent.runner import run_approval

        ops = build_chat_ops(self.cfg, lambda: self.providers, notify=self.notifier.send)
        handled = 0
        for d in self.notifier.poll_once():
            action = d.get("action")

            if action == "chat":
                # Dedupe on the platform's own update id. A restart between
                # fetch and reply, or two pollers racing, would otherwise
                # answer the same message twice.
                uid = d.get("update_id")
                if uid is not None and self._mark(uid):
                    continue
                try:
                    reply = handle_text(d.get("text", ""), self.cfg, self.providers, ops,
                                        chat=d.get("chat", "owner"))
                except Exception as e:
                    log.error("chat command failed: %s: %s", type(e).__name__, e)
                    reply = f"Something went wrong: {type(e).__name__}. Check the logs."
                if reply:
                    self.notifier.send(reply)
                handled += 1
                continue

            key = f"{action}:{d['approval_id']}"
            if self._mark(key):
                # A double-tap still gets its spinner stopped, or the button
                # looks dead even though the first tap worked.
                if d.get("callback_id"):
                    self.notifier.answer_callback(d["callback_id"], "Already decided.")
                continue
            provider = self.providers.get("google") or next(iter(self.providers.values()), None)
            if not provider:
                continue
            try:
                res = run_approval(provider.account, provider, self.cfg, d["approval_id"],
                                   action == "approve", notify=self.notifier.send)
                handled += 1
            except Exception as e:
                log.error("approval handling failed: %s", type(e).__name__)
                res = {"ok": False, "error": type(e).__name__}
            if d.get("callback_id"):
                if res.get("sent"):
                    note = "Sent ✓"
                elif res.get("ok"):
                    note = "Discarded."
                else:
                    note = f"Not sent: {res.get('error', 'held')}"[:190]
                self.notifier.answer_callback(d["callback_id"], note)
                if d.get("callback_msg"):
                    self.notifier.strip_buttons(d.get("callback_chat", ""), d["callback_msg"])
        return handled
