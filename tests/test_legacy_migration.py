from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import sqlite3
from pathlib import Path

import pytest

from relic.checkin.db_init import init_db, seed_facets
from relic.profile import migration
from relic.profile.migration import MigrationError, migrate_legacy_subject
from relic.profile.registry import ProfileRegistry


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _legacy_subject(tmp_path: Path, subject_id: str = "testsubject") -> Path:
    root = tmp_path / "legacy" / subject_id
    root.mkdir(parents=True)
    _write_json(root / "subject_profile.json", {"subject_id": subject_id, "delivery_enabled": True})
    _write_json(
        root / "subject_baseline.json",
        {
            "subject_id": subject_id,
            "interaction": {"directness_preference": {"value": 0.8}},
            "experiment_id": "must-not-land",
        },
    )
    _write_json(
        root / "baseline_user_profile.json",
        {
            "subject_id": subject_id,
            "interaction_preferences": {"message_length": "brief"},
            "self_report_fields": {"preferred_pronoun": {"value": "they"}},
            "researcher_coded_fields": {"secret": "drop"},
        },
    )
    _write_json(
        root / "gumi_background_profile.json",
        {
            "subject_id": subject_id,
            "agent_name": "Gumi",
            "domains": {"identity": {"life": "quiet"}, "boundaries": {"tone": "warm"}},
        },
    )
    _write_json(
        root / "delivery_policy.json",
        {
            "subject_id": subject_id,
            "delivery_enabled": True,
            "maximum_contact_frequency": "1/day",
            "quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Europe/Rome"},
        },
    )
    _write_json(root / "boundary_policy.json", {"subject_id": subject_id, "image_allowed": True})
    _write_json(root / "gumi_generation_constraints.json", {"subject_id": subject_id})
    return root


def _legacy_checkin_store(source: Path, subject_id: str = "testsubject") -> None:
    connection = init_db(source / "relic.db")
    seed_facets(connection)
    connection.execute(
        "INSERT INTO observations "
        "(facet_id, source_type, content, created_at) VALUES (?, ?, ?, ?)",
        ("cognitive.decision_speed", "checkin", "deliberato", "2026-08-01T10:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO checkin_features "
        "(subject_id, tick_id, features_json, posture, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            subject_id,
            "tick-1",
            json.dumps({"subject_id": subject_id, "note": f"parla di {subject_id}"}),
            "ask",
            "2026-08-01T10:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO checkin_cadence_state "
        "(subject_id, non_response_streak, followup_non_response_streak, "
        "diegetic_non_response_streak, updated_at) VALUES (?, ?, ?, ?, ?)",
        (subject_id, 4, 3, 2, "2026-08-01T10:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO passive_extraction_state (subject_id, last_processed_ts, updated_at) "
        "VALUES (?, ?, ?)",
        (subject_id, 10.0, "2026-08-01T10:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    _write_json(
        source / "delivery_policy.json",
        {
            "consent_for_active_elicitation": True,
            "consent_for_passive_extraction": True,
        },
    )


def _pristine_singleton(tmp_path: Path) -> tuple[Path, str]:
    relic_home = tmp_path / "target-relic"
    registry = ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=tmp_path / "target-hermes" / "profiles",
    )
    user = registry.initialize_user()
    checkin = relic_home / "user" / "checkin"
    checkin.mkdir(mode=0o700)
    connection = init_db(checkin / "relic.db")
    seed_facets(connection)
    connection.close()
    _write_json(
        checkin / "delivery_policy.json",
        {
            "consent_for_active_elicitation": False,
            "consent_for_passive_extraction": False,
        },
    )
    return relic_home, user.runtime_scope_id


def test_migration_materializes_subject_scoped_canonical_artifacts(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    relic_home = tmp_path / "target-relic"
    hermes_home = tmp_path / "target-hermes"

    result = migrate_legacy_subject(
        source,
        target_relic_home=relic_home,
        target_hermes_home=hermes_home,
        private_env={
            "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "TELEGRAM_HOME_CHANNEL": "-1001234567890",
        },
    )

    assert result.subject_id == "testsubject"
    registry = ProfileRegistry(relic_home=relic_home, hermes_profiles_home=hermes_home / "profiles")
    artifacts = registry.load_user_artifacts()
    assert artifacts is not None
    assert artifacts["runtime_policy"]["delivery_execution_armed"] is True
    assert artifacts["gumi_profile"]["preferences"]["interaction"]["message_length"] == "brief"
    serialized = json.dumps(artifacts)
    assert "subject" not in serialized.lower()
    assert "experiment" not in serialized.lower()
    assert "researcher" not in serialized.lower()
    assert (relic_home / "user" / "continuity.json").is_file()


def test_dry_run_does_not_write_target(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    relic_home = tmp_path / "target-relic"
    hermes_home = tmp_path / "target-hermes"

    result = migrate_legacy_subject(
        source,
        target_relic_home=relic_home,
        target_hermes_home=hermes_home,
        dry_run=True,
    )

    assert result.dry_run is True
    assert not relic_home.exists()
    assert not hermes_home.exists()


def test_migration_rejects_existing_canonical_target(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    relic_home = tmp_path / "target-relic"
    hermes_home = tmp_path / "target-hermes"
    _write_json(relic_home / "user" / "user_profile.json", {"existing": True})

    with pytest.raises(MigrationError, match="target is not empty"):
        migrate_legacy_subject(
            source,
            target_relic_home=relic_home,
            target_hermes_home=hermes_home,
        )


def test_migration_rejects_wrong_subject_id(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path, "legacysubject")

    with pytest.raises(MigrationError, match="subject-scoped"):
        migrate_legacy_subject(
            source,
            target_relic_home=tmp_path / "target-relic",
            target_hermes_home=tmp_path / "target-hermes",
            expected_subject_id="testsubject",
        )


def test_confirmed_continuity_markers_are_transformed_without_raw_db_copy(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    connection = sqlite3.connect(source / "continuity.db")
    connection.executescript(
        """
        CREATE TABLE continuity_marker (
            marker_id TEXT PRIMARY KEY, subject_id TEXT, gumi_instance_id TEXT,
            hermes_profile_id TEXT, subject_confirmation INTEGER, source_type TEXT,
            created_at TEXT, subject_words_json TEXT, gumi_agreed_words_json TEXT,
            raw_source_text TEXT, status TEXT, gumi_recall_allowed INTEGER,
            recall_count INTEGER, max_recall_count INTEGER, ttl_seconds INTEGER,
            expires_at TEXT, updated_at TEXT, candidate_for_confirmation INTEGER,
            clinical_interpretation_allowed INTEGER, previous_version_id TEXT,
            final_subject_words_json TEXT, next_version_id TEXT, normalized_tags_json TEXT,
            gumi_words_json TEXT
        );
        INSERT INTO continuity_marker VALUES (
            'm1', 'testsubject', 'testsubject', 'testsubject', 1, 'checkin',
            '2026-08-01T10:00:00+00:00', '["hello"]', '["hi"]', NULL,
            'active', 1, 0, 3, 604800, NULL, NULL, 0, 0, NULL, NULL, NULL, NULL, NULL
        );
        """
    )
    connection.commit()
    connection.close()

    relic_home = tmp_path / "target-relic"
    migrate_legacy_subject(
        source,
        target_relic_home=relic_home,
        target_hermes_home=tmp_path / "target-hermes",
    )

    continuity = json.loads((relic_home / "user" / "continuity.json").read_text())
    assert continuity["markers"][0]["hermes_profile_id"] == HERMES_USER_PROFILE_NAME
    assert continuity["markers"][0]["user_confirmation"] is True
    assert not (relic_home / "user" / "continuity.db").exists()


def test_checkin_store_imports_into_pristine_singleton_and_remaps_scope(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    _legacy_checkin_store(source)
    relic_home, scope_id = _pristine_singleton(tmp_path)

    result = migration.migrate_legacy_checkin_store(
        source,
        target_relic_home=relic_home,
    )

    assert result.imported is True
    target_db = relic_home / "user" / "checkin" / "relic.db"
    connection = sqlite3.connect(target_db)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)
    feature_subject, raw_features = connection.execute(
        "SELECT subject_id, features_json FROM checkin_features"
    ).fetchone()
    assert feature_subject == scope_id
    assert json.loads(raw_features) == {
        "subject_id": scope_id,
        "note": "parla di testsubject",
    }
    assert connection.execute(
        "SELECT subject_id, non_response_streak, followup_non_response_streak, "
        "diegetic_non_response_streak FROM checkin_cadence_state"
    ).fetchone() == (scope_id, 0, 0, 0)
    assert connection.execute(
        "SELECT subject_id FROM passive_extraction_state"
    ).fetchone() == (scope_id,)
    assert connection.execute(
        "SELECT state FROM _relic_checkin_migration"
    ).fetchone() == ("complete",)
    connection.close()
    assert json.loads(
        (relic_home / "user" / "checkin" / "delivery_policy.json").read_text()
    ) == {
        "consent_for_active_elicitation": True,
        "consent_for_passive_extraction": True,
    }
    source_connection = sqlite3.connect(source / "relic.db")
    assert source_connection.execute(
        "SELECT subject_id, non_response_streak FROM checkin_cadence_state"
    ).fetchone() == ("testsubject", 4)
    source_connection.close()


def test_checkin_store_rejects_active_unmarked_target(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    _legacy_checkin_store(source)
    relic_home, _scope_id = _pristine_singleton(tmp_path)
    target_db = relic_home / "user" / "checkin" / "relic.db"
    connection = sqlite3.connect(target_db)
    connection.execute(
        "INSERT INTO observations "
        "(facet_id, source_type, content, created_at) VALUES (?, ?, ?, ?)",
        ("cognitive.decision_speed", "message", "live", "2026-08-02T10:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationError, match="active unmarked"):
        migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    connection = sqlite3.connect(target_db)
    assert connection.execute("SELECT content FROM observations").fetchone() == ("live",)
    connection.close()


def test_checkin_store_retry_finishes_pending_policy_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _legacy_subject(tmp_path)
    _legacy_checkin_store(source)
    relic_home, _scope_id = _pristine_singleton(tmp_path)
    original_write = migration._write_checkin_policy

    def fail_policy_write(path: Path, payload: dict[str, bool]) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(migration, "_write_checkin_policy", fail_policy_write)
    with pytest.raises(OSError, match="simulated crash"):
        migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    target_db = relic_home / "user" / "checkin" / "relic.db"
    connection = sqlite3.connect(target_db)
    assert connection.execute(
        "SELECT state FROM _relic_checkin_migration"
    ).fetchone() == ("db_imported",)
    connection.close()

    monkeypatch.setattr(migration, "_write_checkin_policy", original_write)
    result = migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    assert result.imported is False
    connection = sqlite3.connect(target_db)
    assert connection.execute(
        "SELECT state FROM _relic_checkin_migration"
    ).fetchone() == ("complete",)
    assert connection.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)
    connection.close()


def test_completed_checkin_retry_preserves_later_consent_revocation(tmp_path: Path) -> None:
    source = _legacy_subject(tmp_path)
    _legacy_checkin_store(source)
    relic_home, _scope_id = _pristine_singleton(tmp_path)
    migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)
    policy_path = relic_home / "user" / "checkin" / "delivery_policy.json"
    _write_json(
        policy_path,
        {
            "consent_for_active_elicitation": False,
            "consent_for_passive_extraction": False,
        },
    )

    result = migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    assert result.imported is False
    assert json.loads(policy_path.read_text()) == {
        "consent_for_active_elicitation": False,
        "consent_for_passive_extraction": False,
    }


def test_pending_checkin_retry_preserves_consent_revoked_after_policy_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _legacy_subject(tmp_path)
    _legacy_checkin_store(source)
    relic_home, _scope_id = _pristine_singleton(tmp_path)
    original_complete = migration._complete_checkin_migration

    def fail_completion(target_db: Path, source_subject_id: str, runtime_scope_id: str) -> None:
        raise OSError("simulated crash after policy write")

    monkeypatch.setattr(migration, "_complete_checkin_migration", fail_completion)
    with pytest.raises(OSError, match="simulated crash after policy write"):
        migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    policy_path = relic_home / "user" / "checkin" / "delivery_policy.json"
    assert json.loads(policy_path.read_text()) == {
        "consent_for_active_elicitation": True,
        "consent_for_passive_extraction": True,
    }
    _write_json(
        policy_path,
        {
            "consent_for_active_elicitation": False,
            "consent_for_passive_extraction": False,
        },
    )

    monkeypatch.setattr(migration, "_complete_checkin_migration", original_complete)
    result = migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    assert result.imported is False
    assert json.loads(policy_path.read_text()) == {
        "consent_for_active_elicitation": False,
        "consent_for_passive_extraction": False,
    }
    connection = sqlite3.connect(relic_home / "user" / "checkin" / "relic.db")
    assert connection.execute(
        "SELECT state FROM _relic_checkin_migration"
    ).fetchone() == ("complete",)
    connection.close()


@pytest.mark.parametrize("corrupt", [False, True], ids=["missing-schema", "corrupt"])
def test_checkin_store_rejects_invalid_source_database(
    tmp_path: Path, corrupt: bool
) -> None:
    source = _legacy_subject(tmp_path)
    source_db = source / "relic.db"
    if corrupt:
        source_db.write_bytes(b"not a sqlite database")
    else:
        connection = sqlite3.connect(source_db)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
        connection.close()
    relic_home, _scope_id = _pristine_singleton(tmp_path)

    with pytest.raises(MigrationError, match="source check-in database"):
        migration.migrate_legacy_checkin_store(source, target_relic_home=relic_home)

    target = sqlite3.connect(relic_home / "user" / "checkin" / "relic.db")
    assert target.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)
    target.close()
