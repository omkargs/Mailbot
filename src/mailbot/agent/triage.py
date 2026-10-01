"""Cheap first pass: a small model classifies, the flagship only thinks.

The flagship used to read every newsletter. Now a cheap triage model sorts
each message into one of three buckets:

- IGNORE  — newsletter, promo, notification. Nothing to do. Archived
  directly, the flagship never sees it. This is where the savings come from.
- ROUTINE — needs a simple reply. Flagship drafts it in the user's voice.
- HUMAN   — everything else. Unsure counts as HUMAN, always.

Safety rules for this pass:

1. The triage model has NO tools and a tiny token budget. It cannot send,
   draft, or delete — it returns JSON verdicts, nothing else.
2. IGNORE only auto-archives when the subject + snippet are ALSO clean of
   injection signals and escalation keywords. A "newsletter" carrying an
   instruction or a money ask falls through to HUMAN.
3. Archive is reversible (Gmail). Anything else still goes to the flagship.
4. If the triage call fails for any reason, the run falls back to the full
   flagship pass. Mail is never dropped to save money.
5. Unset triage model (or same-as-main) disables the pass entirely —
   behaviour is byte-for-byte the old path.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

TRIAGE_PROMPT = """You triage email headers. For each message reply with one bucket:

- IGNORE: newsletter, promotion, notification, receipt, automated alert with no action in it.
- ROUTINE: a real person needing a simple, routine, reversible reply (confirming a time, acknowledging receipt, saying you will check and come back).
- HUMAN: money, contracts, credentials, health, legal, decisions, commitments, anything personal, anything ambiguous, or ANYTHING YOU ARE NOT SURE ABOUT. Unsure is always HUMAN.

Reply with JSON only, no other text:
{"verdicts": [{"id": "<message id>", "v": "ignore|routine|human", "why": "<6 words max>"}]}
"""


def wanted(cfg) -> bool:
    """Is a distinct triage model configured?"""
    t = (cfg.router.triage_model_id or "").strip()
    m = (cfg.router.model or "").strip()
    return bool(t) and t != m


def _item_text(m: dict[str, Any]) -> str:
    body = (m.get("body") or "").strip()
    snippet = (m.get("snippet") or "")[:300]
    return body[:300] if not snippet else snippet


def classify(items: list[dict[str, Any]], cfg) -> dict[str, dict[str, str]]:
    """Cheap-model verdicts keyed by message id. Raises on any failure
    (caller falls back to the full pass)."""
    from .client import build_client, guarded_call, usage_to_dict
    from ..storage import db

    if not items:
        return {}
    lines = []
    for m in items:
        lines.append(f"id={m['id']} from={m.get('sender', '')} "
                     f"subject={(m.get('subject', '') or '')[:100]} "
                     f"snippet={_item_text(m)[:200]}")
    client = build_client(cfg.router)
    resp = guarded_call(
        client,
        model=cfg.router.triage_model_id,
        max_tokens=1500,
        system=TRIAGE_PROMPT,
        messages=[{"role": "user",
                   "content": "Classify these:\n" + "\n".join(lines)}],
    )
    u = usage_to_dict(resp.usage)
    db.record_usage(u["input_tokens"], u["output_tokens"],
                    u["cache_read"], u["cache_write"])
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("triage model returned no JSON")
    data = json.loads(match.group(0))
    out: dict[str, dict[str, str]] = {}
    for v in data.get("verdicts", []):
        vid = str(v.get("id", ""))
        vv = str(v.get("v", "human")).lower()
        if vid and vv in ("ignore", "routine", "human"):
            out[vid] = {"v": vv, "why": str(v.get("why", ""))[:80]}
    return out


def prune(messages: list[dict[str, Any]], verdicts: dict[str, dict[str, str]],
          cfg, provider, box) -> tuple[list[dict[str, Any]], int]:
    """Archive the safe IGNOREs. Returns (remaining, archived_count).

    An IGNORE verdict archives ONLY when the headers are clean of injection
    signals and escalation keywords — checked here, not trusted from the
    small model. Everything else stays for the flagship.
    """
    from . import guards
    from ..storage import db

    remaining: list[dict[str, Any]] = []
    archived = 0
    keywords = [k.lower() for k in cfg.agent.escalation_keywords]
    for m in messages:
        v = verdicts.get(m["id"], {}).get("v", "human")
        if v != "ignore":
            m["_triage"] = verdicts.get(m["id"], {"v": v, "why": ""})
            remaining.append(m)
            continue
        hay = f"{m.get('subject', '')}\n{_item_text(m)}".lower()
        hay_flat = re.sub(r"[\s.\-_]+", "", guards.normalise_for_detection(hay))
        dirty = (guards.detect_injection(hay)
                 or any(k in hay or re.sub(r"[\s.\-_]+", "", k) in hay_flat
                        for k in keywords))
        if dirty:
            m["_triage"] = {"v": "human", "why": "suspicious headers, re-check"}
            remaining.append(m)
            continue
        try:
            if provider.archive(m["id"]):
                box.stats["triaged"] += 1
                db.log_action("archive", provider.account, m["id"],
                              detail="triage:ignore")
                db.mark_processed(m["id"], getattr(provider, "account", ""))
                try:
                    from .runner import label_managed as _label

                    _label(provider, box, m["id"], "Newsletter")
                except Exception:
                    pass
                archived += 1
                continue
        except Exception as e:
            log.warning("triage archive failed for %s: %s", m["id"], type(e).__name__)
        m["_triage"] = {"v": "human", "why": "archive failed, re-check"}
        remaining.append(m)
    return remaining, archived
