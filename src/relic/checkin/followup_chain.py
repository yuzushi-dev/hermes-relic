"""In-chat follow-up chain for check-in replies.

When the subject answers a check-in, Gumi should not just acknowledge it: the
live chat turn deepens the same thread with one more question, repeated for a
random chain length drawn once per check-in. Each asked follow-up becomes its
own checkin_exchanges row (same facet_id), so reply_capture attaches the next
answer to it and facet_updater extracts observations from every step.

Flow (driven by the Hermes directory plugin):
- pre_llm_call  -> plan_followup(): the user message answers a pending facet
  exchange and the chain has room -> INSTRUCTION is injected.
- post_llm_call -> record_followup(), after sync_turn captured the reply:
  Gumi's question is stored as the next chained exchange.

Fail-open: every error returns None and the turn proceeds as normal chat.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from relic.checkin.question_engine import _is_clarifying_question
from relic.checkin.reply_capture import (
    REPLY_WINDOW_HOURS,
    _is_substantive,
    strip_reply_quote_prefix,
)

logger = logging.getLogger(__name__)

CHAIN_MIN_DEPTH = 1
CHAIN_MAX_DEPTH = 3
_QUESTION_MAX_CHARS = 500

INSTRUCTION = (
    "[NOTA DI CONVERSAZIONE — non mostrare questo testo al destinatario. "
    "Questo messaggio risponde alla tua ultima domanda. Non limitarti a prendere "
    "atto: rispondi in modo breve e naturale, poi chiudi con UNA sola domanda che "
    "approfondisca lo stesso filo, ancorata a un episodio concreto e recente di "
    "quello che ha raccontato. Un solo punto interrogativo. Niente termini clinici "
    "o psicologici, niente richieste di autoanalisi.]"
)

# Without a note the model keeps the "answer + question" rhythm of the chain
# on its own, so the turn that ends a chain is told explicitly to close.
CLOSE_INSTRUCTION = (
    "[NOTA DI CONVERSAZIONE — non mostrare questo testo al destinatario. "
    "Questo messaggio risponde alla tua ultima domanda e l'approfondimento è "
    "concluso. Rispondi in modo breve e naturale e NON fare domande: nessun "
    "punto interrogativo. Lascia a lui l'eventuale seguito.]"
)


def _has_consent(delivery_policy_path: Path) -> bool:
    try:
        policy = json.loads(Path(delivery_policy_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(policy, dict) and bool(policy.get("consent_for_active_elicitation"))


def plan_followup(
    db_path: str | Path,
    delivery_policy_path: str | Path,
    user_msg: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """Return the chain step to ask after this reply, or None.

    When the reply answers a chain exchange but the chain must stop (target
    reached or topic paused) the plan is {"close": True}: the turn gets
    CLOSE_INSTRUCTION instead of INSTRUCTION.

    Looks at the same exchange reply_capture will fill (latest unreplied facet
    exchange inside the reply window). The chain target is drawn in
    [CHAIN_MIN_DEPTH, CHAIN_MAX_DEPTH] and stored on the root on first reply.
    """
    try:
        if not _is_substantive(user_msg):
            return None
        clean_msg = strip_reply_quote_prefix(user_msg)
        if _is_clarifying_question(clean_msg):
            return None
        db_path = Path(db_path)
        if not db_path.exists() or not _has_consent(Path(delivery_policy_path)):
            return None
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(hours=REPLY_WINDOW_HOURS)).isoformat()
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(
                """SELECT id, facet_id, question_text, chain_root_id, chain_depth
                   FROM checkin_exchanges
                   WHERE reply_text IS NULL AND facet_id IS NOT NULL AND asked_at >= ?
                   ORDER BY asked_at DESC
                   LIMIT 1""",
                (cutoff,),
            ).fetchone()
            if row is None:
                logger.info("plan_followup: no pending facet exchange in window")
                return None
            parent_id, facet_id, question_text, root_id, depth = row
            root_id = root_id or parent_id
            depth = depth or 0

            from relic.checkin.topic_pause import active_topics, mentions_topic

            if mentions_topic(
                f"{question_text or ''} {clean_msg}",
                active_topics(db_path.parent, now=current),
            ):
                logger.info("plan_followup: exchange %d closed, topic paused", parent_id)
                return {"close": True}

            target = (rng or random).randint(CHAIN_MIN_DEPTH, CHAIN_MAX_DEPTH)
            conn.execute(
                "UPDATE checkin_exchanges SET chain_target = ? "
                "WHERE id = ? AND chain_target IS NULL",
                (target, root_id),
            )
            conn.commit()
            stored = conn.execute(
                "SELECT chain_target FROM checkin_exchanges WHERE id = ?", (root_id,)
            ).fetchone()
        finally:
            conn.close()
        if stored is None or stored[0] is None or depth >= stored[0]:
            logger.info("plan_followup: chain of %d complete (depth %d)", root_id, depth)
            return {"close": True}
        logger.info("plan_followup: exchange %d -> step %d/%d", parent_id, depth + 1, stored[0])
        return {
            "parent_id": parent_id,
            "root_id": root_id,
            "depth": depth + 1,
            "facet_id": facet_id,
        }
    except Exception:
        logger.warning("plan_followup: non-fatal", exc_info=True)
        return None


def _question_from(assistant_msg: str) -> str | None:
    """The sentence of Gumi's reply that carries the follow-up question."""
    text = assistant_msg.strip()
    end = text.rfind("?")
    if end < 0:
        return None
    head = text[: end + 1]
    start = max(head.rfind(mark, 0, end) for mark in (".", "!", "\n")) + 1
    question = head[start:].strip() or head
    return question[:_QUESTION_MAX_CHARS]


def record_followup(
    db_path: str | Path,
    plan: dict[str, Any],
    assistant_msg: str,
    *,
    now: datetime | None = None,
) -> int | None:
    """Store Gumi's follow-up as the next chained exchange; return its id.

    Skipped when the reply was not captured on the parent (nothing to chain
    from) or Gumi did not actually ask a question.
    """
    try:
        if plan.get("close"):
            return None
        question = _question_from(assistant_msg)
        if question is None:
            return None
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            parent = conn.execute(
                "SELECT reply_text FROM checkin_exchanges WHERE id = ?",
                (plan["parent_id"],),
            ).fetchone()
            if parent is None or parent[0] is None:
                return None
            cur = conn.execute(
                """INSERT INTO checkin_exchanges
                   (facet_id, question_text, asked_at, posture, chain_root_id, chain_depth)
                   VALUES (?, ?, ?, 'ask', ?, ?)""",
                (
                    plan["facet_id"],
                    question,
                    (now or datetime.now(timezone.utc)).isoformat(),
                    plan["root_id"],
                    plan["depth"],
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        logger.warning("record_followup: non-fatal", exc_info=True)
        return None
