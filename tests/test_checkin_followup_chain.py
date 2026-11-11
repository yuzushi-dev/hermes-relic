from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from relic.checkin.db_init import init_db, seed_facets
from relic.checkin.followup_chain import (
    CHAIN_MAX_DEPTH,
    CHAIN_MIN_DEPTH,
    plan_followup,
    record_followup,
)
from relic.checkin.question_engine import select_followup
from relic.checkin.reply_capture import capture_reply_if_pending
from relic.checkin.topic_pause import record_topic_pause

FACET = "aesthetic.music_taste"
REPLY = "Ieri ho ascoltato un vinile di jazz tutto il pomeriggio"


class _FixedRng:
    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, low: int, high: int) -> int:
        assert (low, high) == (CHAIN_MIN_DEPTH, CHAIN_MAX_DEPTH)
        return self.value


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "relic.db"
    conn = init_db(db)
    seed_facets(conn)
    conn.execute(
        "INSERT INTO checkin_exchanges (facet_id, question_text, asked_at, posture) "
        "VALUES (?, 'Che musica stai ascoltando?', ?, 'ask')",
        (FACET, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()),
    )
    conn.commit()
    conn.close()
    policy = tmp_path / "delivery_policy.json"
    policy.write_text(json.dumps({"consent_for_active_elicitation": True}))
    return db, policy


def _turn(db: Path, policy: Path, user_msg: str, assistant_msg: str, rng) -> int | None:
    """One chat turn in hook order: plan, capture (sync_turn), record."""
    plan = plan_followup(db, policy, user_msg, rng=rng)
    capture_reply_if_pending(user_msg, "user", db_path=db, delivery_policy_path=policy)
    return record_followup(db, plan, assistant_msg) if plan else None


def _rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT id, facet_id, reply_text, chain_root_id, chain_depth, chain_target "
            "FROM checkin_exchanges ORDER BY id"
        ).fetchall()


def test_target_depth_is_drawn_in_range(store):
    db, policy = store
    rng = random.Random(0)
    draws = set()
    for _ in range(50):
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE checkin_exchanges SET chain_target = NULL")
        assert plan_followup(db, policy, REPLY, rng=rng) is not None
        draws.add(_rows(db)[0][5])
    assert draws == {1, 2, 3}


@pytest.mark.parametrize("target", [1, 2, 3])
def test_chain_stops_at_target_depth(store, target):
    db, policy = store
    rng = _FixedRng(target)
    for step in range(target):
        new_id = _turn(db, policy, f"{REPLY} {step}", "Bello. E chi c'era con te?", rng)
        assert new_id is not None
    assert plan_followup(db, policy, "Ero da solo in salotto", rng=rng) == {"close": True}

    rows = _rows(db)
    assert len(rows) == target + 1
    root_id = rows[0][0]
    for depth, row in enumerate(rows[1:], start=1):
        assert row[1] == FACET
        assert row[3] == root_id
        assert row[4] == depth
    # Every asked step except the newest got its reply captured.
    assert all(row[2] for row in rows[:-1])


def test_replies_feed_facet_extraction(store):
    db, policy = store
    rng = _FixedRng(2)
    _turn(db, policy, REPLY, "E cosa ti ha colpito di quel disco?", rng)
    _turn(db, policy, "Il contrabbasso, suonava caldissimo", "Lo riascolti spesso?", rng)
    with sqlite3.connect(db) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM checkin_exchanges "
            "WHERE reply_text IS NOT NULL AND observations_extracted = 0 AND facet_id = ?",
            (FACET,),
        ).fetchone()[0]
    assert pending == 2


def test_stored_question_is_gumi_last_question(store):
    db, policy = store
    _turn(db, policy, REPLY, "Che bello! Io adoro il jazz. Quale disco era?", _FixedRng(1))
    with sqlite3.connect(db) as conn:
        question = conn.execute(
            "SELECT question_text FROM checkin_exchanges WHERE chain_depth = 1"
        ).fetchone()[0]
    assert question == "Quale disco era?"


def test_no_row_when_gumi_does_not_ask(store):
    db, policy = store
    assert _turn(db, policy, REPLY, "Che bello, il jazz!", _FixedRng(3)) is None
    assert len(_rows(db)) == 1


@pytest.mark.parametrize("message", ["ok", "Quale musica?", "[silent]"])
def test_no_plan_for_non_answers(store, message):
    db, policy = store
    assert plan_followup(db, policy, message, rng=_FixedRng(3)) is None


def test_no_plan_without_consent(store):
    db, policy = store
    policy.write_text(json.dumps({"consent_for_active_elicitation": False}))
    assert plan_followup(db, policy, REPLY, rng=_FixedRng(3)) is None


def test_paused_topic_closes_instead_of_asking(store):
    db, policy = store
    record_topic_pause(db.parent, "vinile jazz")
    assert plan_followup(db, policy, REPLY, rng=_FixedRng(3)) == {"close": True}


def test_close_plan_records_no_step(store):
    db, policy = store
    assert record_followup(db, {"close": True}, "E poi?") is None
    assert len(_rows(db)) == 1


def test_cron_followup_skips_chain_steps(store):
    db, policy = store
    rng = _FixedRng(1)
    _turn(db, policy, REPLY, "Quale disco era?", rng)
    capture_reply_if_pending("Kind of Blue di Miles Davis", "user", db_path=db, delivery_policy_path=policy)
    with sqlite3.connect(db) as conn:
        assert select_followup(conn) is None
