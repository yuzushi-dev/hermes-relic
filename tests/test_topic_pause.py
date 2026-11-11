import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from relic.checkin import topic_pause
from relic.checkin.context_builder import (
    build_deliver_context,
    build_recent_subject_messages_section,
)
from relic.gumi_plugin.checkin_media_dispatcher import (
    _pause_delivered_topic,
    parse_gate_output,
)

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def test_pause_is_random_between_two_and_seven_days(tmp_path):
    rng = random.Random(1)
    for _ in range(50):
        until = topic_pause.record_topic_pause(tmp_path, "modellino", now=NOW, rng=rng)
        assert timedelta(days=2) <= until - NOW <= timedelta(days=7)
    rows = json.loads((tmp_path / "topic_pauses.json").read_text())
    assert [row["topic"] for row in rows] == ["modellino"]


def test_active_topics_expire(tmp_path):
    until = topic_pause.record_topic_pause(tmp_path, "modellino", now=NOW)
    assert topic_pause.active_topics(tmp_path, now=until - timedelta(minutes=1)) == ["modellino"]
    assert topic_pause.active_topics(tmp_path, now=until + timedelta(minutes=1)) == []


def test_unreadable_store_means_no_pause(tmp_path):
    (tmp_path / "topic_pauses.json").write_text("{not json")
    assert topic_pause.active_topics(tmp_path, now=NOW) == []


def test_mentions_topic_ignores_short_and_stop_words():
    assert topic_pause.mentions_topic("Si, per il modellino 😂", ["modellino"])
    assert topic_pause.mentions_topic("sto montando il modellino", ["il modellino"])
    assert not topic_pause.mentions_topic("il lavoro va bene", ["il modellino"])
    assert not topic_pause.mentions_topic("", ["modellino"])


def test_parse_gate_output_strips_argomento():
    parsed = parse_gate_output("Com'è andato il trasloco?\nargomento: trasloco")
    assert parsed["testo"] == "Com'è andato il trasloco?"
    assert parsed["argomento"] == "trasloco"


def test_pause_recorded_only_for_checkin_and_proactive(tmp_path):
    _pause_delivered_topic(tmp_path, "diario", "diegetic")
    assert not (tmp_path / "checkin" / "topic_pauses.json").exists()
    (tmp_path / "checkin").mkdir()
    _pause_delivered_topic(tmp_path, "modellino", "proactivity")
    assert topic_pause.active_topics(tmp_path / "checkin") == ["modellino"]


def _hermes_home(tmp_path, messages):
    home = tmp_path / "hermes"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (role TEXT, timestamp REAL, content TEXT)")
    for offset, text in enumerate(messages):
        ts = (NOW - timedelta(hours=len(messages) - offset)).timestamp()
        conn.execute("INSERT INTO messages VALUES ('user', ?, ?)", (ts, text))
    conn.commit()
    conn.close()
    return home


def test_recent_messages_drop_paused_topic(tmp_path):
    home = _hermes_home(tmp_path, ["Ho finito il modellino", "Domani vado al mare"])
    section = build_recent_subject_messages_section(
        home, now=NOW, paused_topics=["modellino"]
    )
    assert "mare" in section
    assert "modellino" not in section


def test_deliver_context_lists_paused_topics(tmp_path):
    home = _hermes_home(tmp_path, ["Ho finito il modellino"])
    store = tmp_path / "relic" / "user" / "checkin"
    store.mkdir(parents=True)
    topic_pause.record_topic_pause(store, "modellino", now=NOW)
    context = build_deliver_context(
        "subject",
        home,
        tmp_path / "relic",
        event_type="proactive",
        posture="brief_share",
        db_path=store / "relic.db",
        now=NOW,
    )
    assert "argomenti in pausa" in context
    assert "Ho finito il modellino" not in context
