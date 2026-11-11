"""Canonical User-runtime Chronicle contracts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


_SCOPE_A = "a" * 64
_SCOPE_B = "b" * 64


def _patch_store(monkeypatch: pytest.MonkeyPatch, db_path: str) -> None:
    from relic.chronicle import emitter, reader, retention

    def connection() -> sqlite3.Connection:
        return sqlite3.connect(db_path)

    monkeypatch.setattr(emitter, "_get_db_connection", connection)
    monkeypatch.setattr(reader, "_get_db_connection", connection)
    monkeypatch.setattr(retention, "_get_db_connection", connection)


def test_runtime_scope_round_trip_purge_and_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_relic_db: str,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RELIC_HOME", str(tmp_path))
    _patch_store(monkeypatch, tmp_relic_db)

    from relic.chronicle import EventCategory, emit_event
    from relic.chronicle.reader import query_events
    from relic.chronicle.retention import purge_runtime_scope_records

    first = emit_event(
        runtime_scope_id=_SCOPE_A,
        event_type="scope_a_event",
        event_category=EventCategory.BACKGROUND,
        source_module=__name__,
    )
    second = emit_event(
        runtime_scope_id=_SCOPE_B,
        event_type="scope_b_event",
        event_category=EventCategory.BACKGROUND,
        source_module=__name__,
    )

    assert [row["event_id"] for row in query_events(runtime_scope_id=_SCOPE_A)] == [str(first)]
    assert [row["event_id"] for row in query_events(runtime_scope_id=_SCOPE_B)] == [str(second)]

    result = purge_runtime_scope_records(_SCOPE_A, relic_home=tmp_path)

    assert result["chronicle_events_deleted"] == 1
    assert query_events(runtime_scope_id=_SCOPE_A) == []
    assert [row["event_id"] for row in query_events(runtime_scope_id=_SCOPE_B)] == [str(second)]


def test_legacy_schema_hard_stops_before_any_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "relic.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE chronicle_events (event_id TEXT PRIMARY KEY, subject_id TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("RELIC_HOME", str(tmp_path))

    from relic.chronicle import EventCategory, emit_event
    from relic.chronicle.emitter import LegacyChronicleMigrationRequired

    with pytest.raises(LegacyChronicleMigrationRequired, match="export.*remove"):
        emit_event(
            runtime_scope_id=_SCOPE_A,
            event_type="blocked_legacy_write",
            event_category=EventCategory.BACKGROUND,
            source_module=__name__,
        )

    assert not (tmp_path / "user" / "chronicle" / "journal").exists()


def test_emitted_payload_strips_legacy_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_relic_db: str,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RELIC_HOME", str(tmp_path))
    _patch_store(monkeypatch, tmp_relic_db)

    from relic.chronicle import EventCategory, emit_event

    event_id = emit_event(
        runtime_scope_id=_SCOPE_A,
        event_type="sanitized_event",
        event_category=EventCategory.BACKGROUND,
        source_module=__name__,
        payload={
            "allowed": {"value": True, "risk": "high"},
            "subject_id": "legacy-subject",
            "experiment_id": "legacy-experiment",
            "researcher": "legacy-researcher",
            "consent": "legacy-consent",
            "emergency_contact": "legacy-contact",
        },
    )

    conn = sqlite3.connect(tmp_relic_db)
    payload = json.loads(
        conn.execute(
            "SELECT payload FROM chronicle_events WHERE event_id = ?", (str(event_id),)
        ).fetchone()[0]
    )
    conn.close()
    journal = next((tmp_path / "user" / "chronicle" / "journal").glob("*.jsonl"))
    row = json.loads(journal.read_text(encoding="utf-8"))

    assert payload == {"allowed": {"value": True}}
    assert row["payload"] == payload
    forbidden = {
        "subject_id",
        "experiment_id",
        "researcher",
        "consent",
        "emergency_contact",
        "risk",
    }
    assert forbidden.isdisjoint(row)


def test_cli_query_filters_by_runtime_scope_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from relic.chronicle import reader
    from relic.chronicle.cli.main import main

    query_events = Mock(return_value=[])
    monkeypatch.setattr(reader, "query_events", query_events)

    assert main(
        ["query", "--runtime-scope-id", _SCOPE_A, "--no-audit", "--format", "json"]
    ) == 0
    assert query_events.call_args.kwargs["runtime_scope_id"] == _SCOPE_A