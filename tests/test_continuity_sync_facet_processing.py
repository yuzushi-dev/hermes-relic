"""Phase 3: continuity-sync also drains pending facet check-in replies.

Covers the additive wiring in ``_sync_context`` (src/relic/profile/user_runtime.py)
that, when ``RELIC_SUBJECT_ID`` is set, runs passive extraction and then processes
any pending check-in exchanges against the subject's relic.db/subject_baseline.json.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from relic.profile.registry import ProfileRegistry
from relic.profile.user_runtime import dispatch_action


def _setup_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from relic.gumi_plugin import memory_sync

    registry = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes-profiles",
    )
    user = registry.initialize_user()
    registry.materialize_user_artifacts(
        user,
        baseline={"timezone": "UTC"},
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {
                "proactivity": {
                    "recurring_messages": "on",
                    "quiet_hours": "22:00-08:00",
                    "maximum_frequency": "1/day",
                }
            },
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": True,
            "delivery_execution_armed": True,
            "first_message": "wait",
            "credential_references": {},
        },
        checkin_state={
            "recurring": {"status": "armed"},
            "first_message": {"status": "waiting"},
        },
        private_env={},
    )
    monkeypatch.setenv("HERMES_PROFILES_HOME", str(registry.hermes_profiles_home))
    monkeypatch.setattr(
        memory_sync,
        "sync",
        lambda hermes_home: {
            "scanned": 0,
            "appended": 0,
            "skipped": 0,
            "errors": 0,
            "done": True,
        },
    )
    return registry, user


def _run_sync(registry: ProfileRegistry, user) -> dict[str, str]:
    return dispatch_action(
        "continuity-sync",
        relic_home=registry.relic_home,
        runtime_scope_id=user.runtime_scope_id,
    )


def test_no_subject_id_skips_facet_processing_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, user = _setup_registry(tmp_path, monkeypatch)
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)

    run_for_hermes_home = MagicMock()
    process_pending_exchanges = MagicMock()
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_for_hermes_home", run_for_hermes_home
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges", process_pending_exchanges
    )

    context = _run_sync(registry, user)

    assert context["status"] == "no-change"
    run_for_hermes_home.assert_not_called()
    process_pending_exchanges.assert_not_called()


def test_subject_id_set_with_db_files_present_calls_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, user = _setup_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")

    subject_dir = tmp_path / "subjects" / "testsubject"
    subject_dir.mkdir(parents=True)
    db_path = subject_dir / "relic.db"
    baseline_path = subject_dir / "subject_baseline.json"
    sqlite3.connect(str(db_path)).close()
    baseline_path.write_text("{}", encoding="utf-8")

    run_for_hermes_home = MagicMock()
    process_pending_exchanges = MagicMock(return_value=[])
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_for_hermes_home", run_for_hermes_home
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges", process_pending_exchanges
    )

    context = _run_sync(registry, user)

    assert context["status"] == "no-change"
    run_for_hermes_home.assert_called_once_with(
        registry.hermes_user_home, relic_home=tmp_path, subject_id="testsubject"
    )
    process_pending_exchanges.assert_called_once()
    args, kwargs = process_pending_exchanges.call_args
    assert isinstance(args[0], sqlite3.Connection)
    assert args[1] == baseline_path
    assert args[2] == "testsubject"


def test_subject_id_set_with_db_files_absent_skips_facet_update_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, user = _setup_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    # No subjects/testsubject dir created at all.

    run_for_hermes_home = MagicMock()
    process_pending_exchanges = MagicMock()
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_for_hermes_home", run_for_hermes_home
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges", process_pending_exchanges
    )

    context = _run_sync(registry, user)

    assert context["status"] == "no-change"
    run_for_hermes_home.assert_called_once_with(
        registry.hermes_user_home, relic_home=tmp_path, subject_id="testsubject"
    )
    process_pending_exchanges.assert_not_called()


def test_run_for_hermes_home_raising_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, user = _setup_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")

    subject_dir = tmp_path / "subjects" / "testsubject"
    subject_dir.mkdir(parents=True)
    sqlite3.connect(str(subject_dir / "relic.db")).close()
    (subject_dir / "subject_baseline.json").write_text("{}", encoding="utf-8")

    run_for_hermes_home = MagicMock(side_effect=RuntimeError("boom"))
    process_pending_exchanges = MagicMock()
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_for_hermes_home", run_for_hermes_home
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges", process_pending_exchanges
    )

    context = _run_sync(registry, user)

    assert context["status"] == "no-change"
    run_for_hermes_home.assert_called_once()
    process_pending_exchanges.assert_not_called()


def test_continuity_sync_status_reporting_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: RELIC_SUBJECT_ID unset preserves sync()'s own status reporting."""
    from relic.gumi_plugin import memory_sync

    registry, user = _setup_registry(tmp_path, monkeypatch)
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)
    monkeypatch.setattr(
        memory_sync,
        "sync",
        lambda hermes_home: {
            "scanned": 3,
            "appended": 2,
            "skipped": 0,
            "errors": 0,
            "done": True,
        },
    )

    context = _run_sync(registry, user)
    assert context["status"] == "actionable"
