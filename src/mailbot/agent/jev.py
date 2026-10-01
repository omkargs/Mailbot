"""Jev System-One: the fast decider that sorts mail before the flagship.

The flagship model is slow and costs real money per call, so the original
design had it read every newsletter. Jev answers eight questions about a
message in one request, in milliseconds, for cents:

    needs_user   does a human have to be involved?
    action       ACT (routine, sendable) | ASK (needs the human) | FILE (no reply)
    sensitivity  how bad would a wrong automatic reply be?
    is_cold      stranger outreach or a real conversation?
    needs_reply  is silence rude or costly here?
    deadline     0 = no hurry … 3 = act now
    importance   vip | normal | bulk
    draft_tier   skip | cheap | flagship

The main model then only advises and drafts. Jev decides; `guards` enforces;
a human decides the rest.

Failure is always closed. Every threshold is tunable, and any error, timeout
or low-confidence answer returns ASK — the safest bucket — so mail is never
dropped to save a few cents.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Kept as plain data so the same eight questions are asked every time. A
# question whose wording drifts between runs makes the confidence number
# meaningless: the model would be answering a different question, and the
# threshold would be calibrated against the wrong distribution.
QUESTIONS: dict[str, dict[str, Any]] = {
    "needs_user": {
        "type": "noul",
        "instructions": (
            "This email needs the human recipient to decide, answer a question, "
            "or approve something involving money, contracts, credentials, "
            "health, legal, commitments, or anything personal or uncertain."
        ),
    },
    "action": {
        "type": "choice",
        "instructions": "What should the agent do with this email",
        "criteria": {
            "ACT": "Routine, reversible, unambiguous reply the user would obviously want sent",
            "ASK": "Needs the human: decisions, commitments, sensitive topics, ambiguity",
            "FILE": "No reply needed: newsletters, promos, notifications, FYI",
        },
    },
    "sensitivity": {
        "type": "score",
        "instructions": "How consequential would a wrong automatic reply be",
        "criteria": ["harmless routine", "mildly awkward if wrong",
                     "real consequences", "severe / navigational"],
    },
    "is_cold": {
        "type": "noul",
        "instructions": (
            "This is cold outreach or marketing from a stranger, not a personal conversation"
        ),
    },
    "needs_reply": {
        "type": "noul",
        "instructions": (
            "The sender is waiting on a reply from the user; silence would be rude or costly"
        ),
    },
    "deadline": {
        "type": "score",
        "instructions": "How time-pressured is this email",
        "criteria": ["no hurry", "this week", "within days", "urgent, act now"],
    },
    "importance": {
        "type": "choice",
        "instructions": "Who is this from and how much do they matter",
        "criteria": {
            "vip": "Boss, key client, family, or someone the user answers fast",
            "normal": "Known contact, regular correspondence",
            "bulk": "Mailing list, notification, automated sender",
        },
    },
    "draft_tier": {
        "type": "choice",
        "instructions": "How much brain should write the reply",
        "criteria": {
            "skip": "No reply needed at all",
            "cheap": "Short routine reply, any model can write it",
            "flagship": "Nuanced reply needing the best model and voice care",
        },
    },
}

ACT, ASK, FILE = "ACT", "ASK", "FILE"


class JevVerdict:
    """One message's verdict plus the signals that produced it."""

    __slots__ = ("verdict", "confidence", "needs_user", "sensitivity", "is_cold",
                 "needs_reply", "deadline", "importance", "draft_tier", "reason",
                 "raw")

    def __init__(self, verdict: str = ASK, confidence: float = 0.0, **kw: Any):
        self.verdict = verdict
        self.confidence = confidence
        self.needs_user = float(kw.get("needs_user") or 0.0)
        self.sensitivity = float(kw.get("sensitivity") or 0.0)
        self.is_cold = float(kw.get("is_cold") or 0.0)
        self.needs_reply = float(kw.get("needs_reply") or 0.0)
        self.deadline = float(kw.get("deadline") or 0.0)
        self.importance = str(kw.get("importance") or "normal")
        self.draft_tier = str(kw.get("draft_tier") or "flagship")
        self.reason = str(kw.get("reason") or "")
        self.raw = kw.get("raw") or {}

    @property
    def is_vip(self) -> bool:
        return self.importance == "vip"

    @property
    def is_urgent(self) -> bool:
        return self.deadline >= 2

    @property
    def flags(self) -> list[str]:
        """Card annotations. Empty when nothing is worth pinning."""
        out = []
        if self.is_vip:
            out.append("VIP")
        if self.is_urgent:
            out.append("urgent")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "confidence": self.confidence,
            "needs_user": self.needs_user, "sensitivity": self.sensitivity,
            "is_cold": self.is_cold, "needs_reply": self.needs_reply,
            "deadline": self.deadline, "importance": self.importance,
            "draft_tier": self.draft_tier, "reason": self.reason,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JevVerdict {self.verdict} conf={self.confidence:.2f}>"


def _as_float(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    # NaN compares false against everything, so a NaN confidence would sail
    # past `conf < min_confidence` and be treated as a confident verdict.
    return 0.0 if f != f else f


def interpret(resp: dict, cfg) -> JevVerdict:
    """Map a raw Jev response onto a verdict. Pure, so it is directly testable.

    The rules here are the safety policy, not formatting:
      * an unrecognised action is ASK, never a guess
      * low confidence is ASK
      * a strong needs-human signal is ASK whatever the action said
      * a genuinely sensitive message is ASK even at high confidence
      * FILE is only honoured when nothing wants the human
    """
    a = (resp or {}).get("answers") or {}
    action = a.get("action") or {}
    verdict = str(action.get("choice") or ASK).upper()
    conf = _as_float(action.get("confidence"))
    needs = _as_float((a.get("needs_user") or {}).get("noul"))
    sens = _as_float((a.get("sensitivity") or {}).get("score"))
    cold = _as_float((a.get("is_cold") or {}).get("noul"))

    v = JevVerdict(
        verdict=verdict if verdict in (ACT, ASK, FILE) else ASK,
        confidence=conf,
        needs_user=needs,
        sensitivity=sens,
        is_cold=cold,
        needs_reply=_as_float((a.get("needs_reply") or {}).get("noul")),
        deadline=_as_float((a.get("deadline") or {}).get("score")),
        importance=str((a.get("importance") or {}).get("choice") or "normal"),
        draft_tier=str((a.get("draft_tier") or {}).get("choice") or "flagship"),
        raw=(resp or {}).get("usage") or {},
    )

    if v.verdict not in (ACT, ASK, FILE):
        v.verdict, v.reason = ASK, "unrecognised action"
        return v
    if v.confidence < cfg.jev.min_confidence:
        v.verdict = ASK
        v.reason = f"low confidence ({conf:.2f} < {cfg.jev.min_confidence:.2f})"
        return v
    if needs > cfg.jev.needs_cut:
        v.verdict = ASK
        v.reason = f"needs the human ({needs:.2f})"
        return v
    if v.verdict == ACT and sens >= 2:
        # Sensitive enough that a wrong automatic reply has real consequences.
        v.verdict = ASK
        v.reason = f"sensitivity {sens:.0f}/3"
        return v
    if v.verdict == FILE and needs > cfg.jev.file_needs_cut:
        v.verdict = ASK
        v.reason = f"filing would drop something the human wants ({needs:.2f})"
        return v
    if not v.reason:
        v.reason = f"jev {v.verdict.lower()} at {conf:.2f}"
    return v


def state_for(mail: dict[str, Any]) -> str:
    """The text Jev judges. Headers plus a body slice, nothing else."""
    body = (mail.get("body") or mail.get("snippet") or "").strip()
    return (
        f"From: {mail.get('sender', '')}\n"
        f"Subject: {mail.get('subject', '')}\n\n"
        f"{body[:2000]}"
    )


def decide(mail: dict[str, Any], cfg) -> JevVerdict:
    """Ask Jev about one message. Never raises — failure is ASK."""
    import requests

    url, key, model = cfg.jev.endpoint(cfg.router)
    if not (url and key):
        return JevVerdict(ASK, 0.0, reason="jev not configured")
    try:
        r = requests.post(
            f"{url}/v1/systemone",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "state": state_for(mail)[:4000],
                  "questions": QUESTIONS},
            timeout=cfg.jev.timeout,
        )
        r.raise_for_status()
        return interpret(r.json(), cfg)
    except Exception as e:
        # Fail closed. A decider that cannot be reached must not become a
        # decider that files everything.
        log.warning("jev failed (%s); holding for the user", type(e).__name__)
        return JevVerdict(ASK, 0.0, reason=f"jev unavailable: {type(e).__name__}")


def enabled(cfg) -> bool:
    return bool(getattr(cfg.jev, "enabled", False)) and cfg.jev.configured(cfg.router)


def prune(
    messages: list[dict[str, Any]],
    verdicts: dict[str, JevVerdict],
    cfg,
    provider,
    box,
) -> tuple[list[dict[str, Any]], int]:
    """File the safe FILE verdicts. Returns (remaining, archived_count).

    A FILE verdict archives ONLY when the subject and snippet are also clean
    of injection signals and escalation keywords — checked here, in code, not
    trusted from the decider. A "newsletter" that carries an instruction or a
    money ask falls through to the flagship instead of disappearing.
    """
    import re

    from . import guards
    from ..storage import db

    keywords = [k.lower() for k in cfg.agent.escalation_keywords]
    remaining: list[dict[str, Any]] = []
    archived = 0

    for m in messages:
        v = verdicts.get(m["id"])
        if v is None:
            remaining.append(m)
            continue
        # Everything Jev knows rides along to the flagship and the card.
        m["_jev"] = v
        if v.verdict != FILE:
            remaining.append(m)
            continue

        hay = f"{m.get('subject', '')}\n{m.get('snippet') or m.get('body') or ''}"
        hay_flat = re.sub(r"[\s.\-_]+", "", guards.normalise_for_detection(hay))
        dirty = (
            guards.detect_injection(hay)
            or any(k in hay.lower() or re.sub(r"[\s.\-_]+", "", k) in hay_flat
                   for k in keywords)
        )
        if dirty:
            # Suspicious headers on a "no reply needed" verdict. Not filed.
            m["_jev"] = JevVerdict(ASK, v.confidence, needs_user=v.needs_user,
                                   sensitivity=v.sensitivity, is_cold=v.is_cold,
                                   needs_reply=v.needs_reply, deadline=v.deadline,
                                   importance=v.importance, draft_tier=v.draft_tier,
                                   reason="suspicious headers, held for review")
            remaining.append(m)
            continue

        try:
            if provider.archive(m["id"]):
                box.stats["triaged"] += 1
                db.log_action("archive", provider.account, m["id"],
                              detail=f"jev:file ({v.confidence:.2f})")
                db.mark_processed(m["id"], getattr(provider, "account", ""))
                archived += 1
                continue
        except Exception as e:
            log.warning("jev archive failed for %s: %s", m["id"], type(e).__name__)
        m["_jev"] = JevVerdict(ASK, v.confidence, needs_user=v.needs_user,
                               sensitivity=v.sensitivity, is_cold=v.is_cold,
                               needs_reply=v.needs_reply, deadline=v.deadline,
                               importance=v.importance, draft_tier=v.draft_tier,
                               reason="archive failed, held for review")
        remaining.append(m)

    return remaining, archived
