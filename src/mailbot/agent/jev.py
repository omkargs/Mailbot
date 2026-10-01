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
    # --- the autonomy six: what the extra questions buy ---
    # Latency is flat per call (~2s for 8 or 16 questions), so these are
    # free. Each one removes a class of approval spam or a class of
    # cowardly escalation.
    "reply_shape": {
        "type": "choice",
        "instructions": "If a reply is warranted, how much reply does it need",
        "criteria": {
            "none": "No reply at all",
            "short": "A sentence or two closes it",
            "full": "Needs a real, considered reply",
        },
    },
    "money_involved": {
        "type": "noul",
        "instructions": "Money, payments, contracts, prices, or financial commitment are involved",
    },
    "commitment": {
        "type": "noul",
        "instructions": "A reply would commit the user to a plan, a price, a yes/no, or an opinion",
    },
    "emotion_heat": {
        "type": "score",
        "instructions": "How emotionally loaded this is — anger, grief, conflict, effusive thanks",
        "criteria": ["flat routine", "warm human", "emotional", "handle with real care"],
    },
    "knows_user": {
        "type": "noul",
        "instructions": "The sender clearly knows the user personally, not a stranger or a list",
    },
    "thread_continuation": {
        "type": "choice",
        "instructions": "Where this sits in a conversation",
        "criteria": {
            "new": "Opens a new conversation",
            "continuation": "Continues an existing back-and-forth",
            "broadcast": "One-to-many mail, no conversation to continue",
        },
    },
}

ACT, ASK, FILE = "ACT", "ASK", "FILE"


class JevVerdict:
    """One message's verdict plus the signals that produced it."""

    __slots__ = ("verdict", "confidence", "needs_user", "sensitivity", "is_cold",
                 "needs_reply", "deadline", "importance", "draft_tier", "reason",
                 "raw", "p_act", "p_ask", "margin", "money", "commitment",
                 "emotion", "knows_user", "thread_continuation", "reply_shape",
                 "deadline_p3", "auto_ok")

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
        # Margin routing: how far ahead the winner is, and how strong the
        # runner-up is. A 0.45/0.44 ACT "win" is a coin flip, not a verdict.
        self.p_act = float(kw.get("p_act") or 0.0)
        self.p_ask = float(kw.get("p_ask") or 0.0)
        self.margin = float(kw.get("margin") or 0.0)
        # Autonomy six. All 0–1 except emotion (0–3 like deadline).
        self.money = float(kw.get("money") or 0.0)
        self.commitment = float(kw.get("commitment") or 0.0)
        self.emotion = float(kw.get("emotion") or 0.0)
        self.knows_user = float(kw.get("knows_user") or 0.0)
        self.thread_continuation = str(kw.get("thread_continuation") or "new")
        self.reply_shape = str(kw.get("reply_shape") or "full")
        self.deadline_p3 = float(kw.get("deadline_p3") or 0.0)
        # Set by interpret(): may this verdict endorse an unattended send?
        self.auto_ok = bool(kw.get("auto_ok", False))

    @property
    def is_vip(self) -> bool:
        return self.importance == "vip"

    @property
    def is_urgent(self) -> bool:
        # The distribution knows more than the point estimate: a 1.8 with
        # half its mass on "act now" is more urgent than a flat 2.0.
        return self.deadline >= 2 or self.deadline_p3 >= 0.5

    @property
    def flags(self) -> list[str]:
        """Card annotations. Empty when nothing is worth pinning."""
        out = []
        if self.is_vip:
            out.append("VIP")
        if self.is_urgent:
            out.append("urgent")
        return out

    @property
    def plain_reason(self) -> str:
        """The reason in human words. The machine reason keeps its numbers
        (logs, ledger, /jev) — the card does not. Nobody was ever reassured
        by "(0.52 < 0.60)".
        """
        import re

        r = re.sub(r"\s*\(.*?\)", "", self.reason or "").strip()
        mapping = [
            ("low confidence", "not sure what this one wants"),
            ("needs the human", "this one needs you"),
            ("contested judgment", "my two reads disagreed"),
            ("coin-flip verdict", "too close to call"),
            ("money involved", "money involved — your call"),
            ("reply would commit you", "answering would commit you to something"),
            ("sensitivity", "too sensitive to auto-handle"),
            ("emotionally loaded", "emotionally loaded — needs a human"),
            ("second jev call failed", "my double-check failed, holding it"),
            ("unrecognised action", "couldn't judge it"),
            ("jev unavailable", "decider was unreachable, holding it"),
            ("jev not configured", "decider off, holding it"),
            ("suspicious headers", "headers look suspicious"),
            ("archive failed", "couldn't file it, holding it"),
            ("filing would drop", "looks like it needs you"),
        ]
        for prefix, plain in mapping:
            if r.startswith(prefix):
                return plain
        return r or "needs you"

    @property
    def confident_auto(self) -> bool:
        """May this verdict endorse an unattended send by itself?"""
        return self.auto_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "confidence": self.confidence,
            "needs_user": self.needs_user, "sensitivity": self.sensitivity,
            "is_cold": self.is_cold, "needs_reply": self.needs_reply,
            "deadline": self.deadline, "importance": self.importance,
            "draft_tier": self.draft_tier, "reason": self.reason,
            "p_act": self.p_act, "p_ask": self.p_ask, "margin": self.margin,
            "money": self.money, "commitment": self.commitment,
            "emotion": self.emotion, "knows_user": self.knows_user,
            "thread_continuation": self.thread_continuation,
            "reply_shape": self.reply_shape,
            "deadline_p3": self.deadline_p3, "auto_ok": self.auto_ok,
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
      * a coin-flip ACT (thin margin, strong ASK minority) is ASK — a
        0.45/0.44 "win" is not a verdict, and acting on it is how agents send
        mail the user then has to apologise for
      * money, commitment or real emotion on an ACT is ASK
      * FILE is only honoured when nothing wants the human
    """
    a = (resp or {}).get("answers") or {}
    action = a.get("action") or {}
    verdict = str(action.get("choice") or ASK).upper()
    conf = _as_float(action.get("confidence"))
    probs = action.get("probabilities") or {}
    p_act = _as_float(probs.get("ACT", conf if verdict == ACT else 0.0))
    p_ask = _as_float(probs.get("ASK", conf if verdict == ASK else 0.0))
    ordered = sorted((_as_float(x) for x in probs.values()), reverse=True)
    margin = (ordered[0] - ordered[1]) if len(ordered) > 1 else conf
    needs = _as_float((a.get("needs_user") or {}).get("noul"))
    sens = _as_float((a.get("sensitivity") or {}).get("score"))
    cold = _as_float((a.get("is_cold") or {}).get("noul"))
    deadline = a.get("deadline") or {}
    d_probs = deadline.get("probabilities") or {}
    money = _as_float((a.get("money_involved") or {}).get("noul"))
    commitment = _as_float((a.get("commitment") or {}).get("noul"))
    emotion = _as_float((a.get("emotion_heat") or {}).get("score"))
    tier = str((a.get("draft_tier") or {}).get("choice") or "flagship")
    shape = str((a.get("reply_shape") or {}).get("choice") or "full")
    if shape == "short" and tier == "flagship":
        # A two-sentence reply does not need the best model and the full
        # voice pass. Downgrading here is where the flagship savings come from.
        tier = "cheap"

    v = JevVerdict(
        verdict=verdict if verdict in (ACT, ASK, FILE) else ASK,
        confidence=conf,
        needs_user=needs,
        sensitivity=sens,
        is_cold=cold,
        needs_reply=_as_float((a.get("needs_reply") or {}).get("noul")),
        deadline=_as_float(deadline.get("score")),
        importance=str((a.get("importance") or {}).get("choice") or "normal"),
        draft_tier=tier,
        p_act=p_act, p_ask=p_ask, margin=margin,
        money=money, commitment=commitment, emotion=emotion,
        knows_user=_as_float((a.get("knows_user") or {}).get("noul")),
        thread_continuation=str((a.get("thread_continuation") or {}).get("choice") or "new"),
        reply_shape=shape,
        deadline_p3=_as_float(d_probs.get("3")),
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
    if v.verdict == ACT and p_ask >= cfg.jev.ask_prob_floor:
        # A strong ASK minority means the judgment itself is contested.
        # Acting on a contested judgment is guessing with extra steps.
        v.verdict = ASK
        v.reason = f"contested judgment (P(ask) {p_ask:.2f})"
        return v
    if v.verdict == ACT and margin < cfg.jev.min_margin:
        v.verdict = ASK
        v.reason = f"coin-flip verdict (margin {margin:.2f})"
        return v
    if v.verdict == ACT and money >= 0.5:
        v.verdict = ASK
        v.reason = f"money involved ({money:.2f})"
        return v
    if v.verdict == ACT and commitment >= 0.5:
        v.verdict = ASK
        v.reason = f"reply would commit you ({commitment:.2f})"
        return v
    if v.verdict != FILE and emotion >= 2:
        v.verdict = ASK
        v.reason = f"emotionally loaded ({emotion:.0f}/3)"
        return v
    if v.verdict == FILE and needs > cfg.jev.file_needs_cut:
        v.verdict = ASK
        v.reason = f"filing would drop something the human wants ({needs:.2f})"
        return v
    # The autonomy endorsement. Consequence is the bar, not familiarity:
    # high-margin ACT, nothing financial, social or emotional load-bearing.
    # Guards still enforce injection, escalation, attachments and the
    # new-thread gate on top — this opens the contact-standing gate only.
    v.auto_ok = (
        v.verdict == ACT
        and p_act >= cfg.jev.act_p
        and margin >= cfg.jev.auto_margin
        and sens < 1
        and cold < 0.5
        and money < 0.5
        and commitment < 0.5
    )
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


def _ask_once(state: str, url: str, key: str, model: str, timeout: int) -> dict:
    """One System-One call. Raises on any failure — the caller decides."""
    import requests

    r = requests.post(
        f"{url}/v1/systemone",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "state": state[:4000], "questions": QUESTIONS},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def combine(first: JevVerdict, second: JevVerdict, cfg) -> JevVerdict:
    """Merge two independent judgments on the same mail. Pure and testable.

    Agreement strengthens: confidence and margin take the weaker of the two,
    so one bullish call cannot launder a doubtful one. Any disagreement on
    the action holds the message — two deciders that cannot agree are the
    definition of "needs the human". The merged verdict keeps the strongest
    urgency and need signals from either call, because dropping a VIP flag
    or a deadline one call saw would be losing information to consensus.
    """
    if first.verdict == second.verdict:
        out = JevVerdict(
            verdict=first.verdict,
            confidence=min(first.confidence, second.confidence),
            needs_user=max(first.needs_user, second.needs_user),
            sensitivity=max(first.sensitivity, second.sensitivity),
            is_cold=max(first.is_cold, second.is_cold),
            needs_reply=max(first.needs_reply, second.needs_reply),
            deadline=max(first.deadline, second.deadline),
            importance="vip" if "vip" in (first.importance, second.importance)
            else first.importance,
            draft_tier=("flagship" if "flagship" in (first.draft_tier, second.draft_tier)
                        else first.draft_tier),
            p_act=min(first.p_act, second.p_act),
            p_ask=max(first.p_ask, second.p_ask),
            margin=min(first.margin, second.margin),
            money=max(first.money, second.money),
            commitment=max(first.commitment, second.commitment),
            emotion=max(first.emotion, second.emotion),
            knows_user=max(first.knows_user, second.knows_user),
            thread_continuation=(first.thread_continuation
                                 if first.thread_continuation == second.thread_continuation
                                 else "new"),
            reply_shape=(first.reply_shape
                         if first.reply_shape == second.reply_shape else "full"),
            deadline_p3=max(first.deadline_p3, second.deadline_p3),
            reason=f"{first.reason} (2/2 agree)",
            raw=first.raw,
        )
        # Endorsement needs both calls independently consequence-free.
        out.auto_ok = bool(first.auto_ok and second.auto_ok)
        return out
    out = JevVerdict(
        verdict=ASK,
        confidence=min(first.confidence, second.confidence),
        needs_user=max(first.needs_user, second.needs_user),
        sensitivity=max(first.sensitivity, second.sensitivity),
        is_cold=max(first.is_cold, second.is_cold),
        needs_reply=max(first.needs_reply, second.needs_reply),
        deadline=max(first.deadline, second.deadline),
        importance="vip" if "vip" in (first.importance, second.importance)
        else "normal",
        draft_tier="flagship",
        p_act=(first.p_act + second.p_act) / 2,
        p_ask=(first.p_ask + second.p_ask) / 2,
        margin=0.0,
        money=max(first.money, second.money),
        commitment=max(first.commitment, second.commitment),
        emotion=max(first.emotion, second.emotion),
        knows_user=max(first.knows_user, second.knows_user),
        thread_continuation="new",
        reply_shape="full",
        deadline_p3=max(first.deadline_p3, second.deadline_p3),
        reason=f"jev disagreed ({first.verdict} vs {second.verdict}) — held",
        raw=first.raw,
    )
    out.auto_ok = False
    return out


def decide(mail: dict[str, Any], cfg) -> JevVerdict:
    """Ask Jev about one message — twice, independently. Never raises.

    Double verification: two calls, two judgments, one verdict. Agreement
    strengthens (at the weaker call's confidence); disagreement holds the
    mail. A single stochastic judgment is an opinion; two that agree are
    evidence. Disable with JEV_DOUBLE_CHECK=0, at the cost of exactly the
    safety this exists for.
    """
    url, key, model = cfg.jev.endpoint(cfg.router)
    if not (url and key):
        return JevVerdict(ASK, 0.0, reason="jev not configured")
    state = state_for(mail)
    try:
        first = interpret(_ask_once(state, url, key, model, cfg.jev.timeout), cfg)
    except Exception as e:
        # Fail closed. A decider that cannot be reached must not become a
        # decider that files everything.
        log.warning("jev failed (%s); holding for the user", type(e).__name__)
        return JevVerdict(ASK, 0.0, reason=f"jev unavailable: {type(e).__name__}")
    if not getattr(cfg.jev, "double_check", True):
        return first
    try:
        second = interpret(_ask_once(state, url, key, model, cfg.jev.timeout), cfg)
    except Exception as e:
        # One good judgment plus one failure is not verification. The first
        # call may still be right, but "may" is not the standard — hold it,
        # and say which half failed.
        log.warning("jev second call failed (%s); holding for the user", type(e).__name__)
        out = JevVerdict(ASK, first.confidence, needs_user=first.needs_user,
                         sensitivity=first.sensitivity, is_cold=first.is_cold,
                         needs_reply=first.needs_reply, deadline=first.deadline,
                         importance=first.importance, draft_tier="flagship",
                         p_act=first.p_act, p_ask=first.p_ask, margin=0.0,
                         money=first.money, commitment=first.commitment,
                         emotion=first.emotion, knows_user=first.knows_user,
                         reason=f"second jev call failed ({type(e).__name__}) — held")
        out.auto_ok = False
        return out
    return combine(first, second, cfg)


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
                try:
                    from .runner import label_managed as _label

                    if _label(provider, box, m["id"], "Newsletter"):
                        db.log_action("label", provider.account, m["id"],
                                      detail="Newsletter")
                except Exception:
                    pass
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
