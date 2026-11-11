from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from relic.profile.registry import ProfileRegistry
from relic.profile.user_runtime import (
    _CanonicalRuntime,
    _load_delivery_state,
    _load_streak_state,
    _non_response_backoff_blocks,
    _reserve_delivery,
    _write_streak_state,
    consume_delivery_reservation,
    dispatch_action,
    record_delivery_outcome,
    record_subject_reply,
)


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    maximum_frequency: str = "2/day",
    checkin_slots: list[str] | None = None,
    setup_mode: str = "default",
    diegetic_enabled: bool = False,
) -> tuple[ProfileRegistry, str]:
    monkeypatch.setenv("HERMES_PROFILES_HOME", str(tmp_path / "hermes-profiles"))
    registry = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes-profiles",
    )
    user = registry.initialize_user()
    preferences: dict = {
        "proactivity": {
            "recurring_messages": "on",
            "after_no_reply": "pause",
            "quiet_hours": "22:00-08:00",
            "maximum_frequency": maximum_frequency,
        }
    }
    safety: dict[str, object] = {"diegetic_enabled": diegetic_enabled}
    if checkin_slots is not None:
        safety["checkin_slots"] = checkin_slots
    preferences["safety"] = safety
    registry.materialize_user_artifacts(
        user,
        baseline={
            "timezone": "UTC",
            "item_battery": {
                "scores": {
                    "project_calibration": {
                        "fictional_diegesis_tolerance": 0.8
                    }
                }
            },
        },
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": setup_mode,
            "preferences": preferences,
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
    policy_path = tmp_path / "user" / "checkin" / "delivery_policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        '{"consent_for_active_elicitation": true, '
        '"consent_for_passive_extraction": true}\n',
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    _write_message_job_manifest(tmp_path)
    return registry, user.runtime_scope_id


def _queue_candidate(home: Path, *, priority: float = 0.9) -> None:
    queue = home / "user" / "checkin" / "proactive_queue.jsonl"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        '{"id":"candidate","priority":'
        + str(priority)
        + ',"signal_ref":"test","suggested_posture":"brief_share"}\n',
        encoding="utf-8",
    )
    queue.chmod(0o600)


def _run(action: str, home: Path, scope_id: str, hour: int, minute: int = 0) -> dict[str, str]:
    return dispatch_action(
        action,
        relic_home=home,
        runtime_scope_id=scope_id,
        now=datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc),
    )


def test_checkin_respects_window_and_policy_question_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch)
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME

    assert _run("checkin-gate", tmp_path, scope_id, 12)["status"] == "eligible"
    assert _run("checkin-message", tmp_path, scope_id, 12)["status"] == "ready"
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert _run("checkin-gate", tmp_path, scope_id, 12, 30)["status"] == "silent"
    assert _run("checkin-gate", tmp_path, scope_id, 13)["status"] == "silent"
    assert _run("checkin-gate", tmp_path, scope_id, 18)["status"] == "silent"


def test_checkin_respects_daily_frequency_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch)
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME

    assert _run("checkin-message", tmp_path, scope_id, 10)["status"] == "ready"
    consume_delivery_reservation(
        hermes_home,
        "cron_checkinjob01_20260101_100000",
        relic_home=tmp_path,
    )
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
    with sqlite3.connect(tmp_path / "user" / "checkin" / "relic.db") as connection:
        connection.execute(
            "UPDATE checkin_exchanges SET reply_text = ?, reply_captured_at = ?",
            ("risposta", datetime(2026, 1, 1, 11, tzinfo=timezone.utc).isoformat()),
        )
        connection.commit()
    assert _run("checkin-message", tmp_path, scope_id, 16)["status"] == "ready"
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 16, tzinfo=timezone.utc))
    assert _run("checkin-gate", tmp_path, scope_id, 19)["status"] == "silent"
    assert _run("checkin-gate", tmp_path, scope_id, 23)["status"] == "silent"


def test_proactive_and_checkin_messages_share_delivery_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch, setup_mode="legacy_import")
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    _queue_candidate(tmp_path)

    assert _run("proactive-message", tmp_path, scope_id, 10)["status"] == "ready"
    consume_delivery_reservation(
        hermes_home,
        "cron_proactjob001_20260101_100000",
        relic_home=tmp_path,
    )
    record_delivery_outcome(hermes_home, "proactivity", datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
    assert _run("checkin-gate", tmp_path, scope_id, 12)["status"] == "silent"
    assert _run("checkin-message", tmp_path, scope_id, 15)["status"] == "ready"
    consume_delivery_reservation(
        hermes_home,
        "cron_checkinjob01_20260101_150000",
        relic_home=tmp_path,
    )
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 15, tzinfo=timezone.utc))
    assert _run("proactive-gate", tmp_path, scope_id, 20)["status"] == "silent"


def test_canonical_proactive_message_requires_queued_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch)

    result = _run("proactive-message", tmp_path, scope_id, 10)

    assert result["status"] == "silent"
    assert result["wakeAgent"] is False


def test_forced_diegetic_gate_bypasses_policy(monkeypatch, tmp_path: Path) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch, diegetic_enabled=False)
    monkeypatch.setenv("RELIC_FORCE_CHECKIN", "1")

    result = dispatch_action(
        "diegetic-gate",
        relic_home=tmp_path,
        runtime_scope_id=scope_id,
        now=datetime(2026, 1, 1, 15, tzinfo=timezone.utc),
    )

    assert result["status"] == "eligible"
    assert result["wakeAgent"] is True
    assert result["policy_reason"] == "forced"


def test_forced_diegetic_gate_honors_forced_media_type(
    monkeypatch, tmp_path: Path
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch, diegetic_enabled=False)
    monkeypatch.setenv("RELIC_FORCE_CHECKIN", "1")
    monkeypatch.setenv("RELIC_FORCE_MEDIA_TYPE", "voice")

    result = dispatch_action(
        "diegetic-gate",
        relic_home=tmp_path,
        runtime_scope_id=scope_id,
        now=datetime(2026, 1, 1, 15, tzinfo=timezone.utc),
    )

    assert result["gate"].splitlines()[1] == "tipo: voice"


def test_diegetic_message_shares_daily_delivery_cap_with_checkin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch, maximum_frequency="1/day")
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME

    assert _run("checkin-message", tmp_path, scope_id, 10)["status"] == "ready"
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
    assert _run("diegetic-gate", tmp_path, scope_id, 15)["status"] == "silent"
    assert _run("diegetic-message", tmp_path, scope_id, 15)["status"] == "silent"


def test_initiative_lanes_share_oss_three_hour_spacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(
        tmp_path,
        monkeypatch,
        maximum_frequency="3/day",
        diegetic_enabled=True,
    )
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    _queue_candidate(tmp_path)

    assert _run("diegetic-message", tmp_path, scope_id, 9, 30)["status"] == "ready"
    record_delivery_outcome(
        hermes_home, "diegetic", datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
    )
    assert _run("proactive-gate", tmp_path, scope_id, 12, 30)["status"] == "eligible"
    assert _run("checkin-gate", tmp_path, scope_id, 17, 30)["status"] == "eligible"

def test_guard_uses_active_hermes_profile_from_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_PROFILES_HOME")
    active_profile = tmp_path / "live-hermes" / "profiles" / HERMES_USER_PROFILE_NAME
    monkeypatch.setenv("HERMES_HOME", str(active_profile))

    assert _run("checkin-message", tmp_path, scope_id, 12)["status"] == "ready"
    assert (active_profile / "state" / "checkin_guard.json").exists()
    assert not (
        tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME / "state" / "checkin_guard.json"
    ).exists()



def _minimal_runtime(tmp_path: Path) -> _CanonicalRuntime:
    runtime = _CanonicalRuntime(
        scope_id="scope",
        relic_home=tmp_path,
        hermes_home=tmp_path / "hermes",
        artifacts={},
    )
    _write_message_job_manifest(tmp_path)
    return runtime


_MESSAGE_JOB_IDS = {
    "gumi_checkin_message": "checkinjob01",
    "gumi_proactive_message": "proactjob001",
    "gumi_diegetic_message": "diegetic001",
}


def _write_message_job_manifest(relic_home: Path) -> None:
    path = relic_home / "user" / "managed_cron.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {
                    name: {"id": job_id, "fingerprint": "test"}
                    for name, job_id in _MESSAGE_JOB_IDS.items()
                },
            }
        ),
        encoding="utf-8",
    )


def test_reserve_delivery_persists_kind_for_checkin_proactivity_and_diegetic(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"

    _reserve_delivery(runtime, now, "checkin-message")
    deliveries, pending_at, kind = _load_delivery_state(state_path)
    assert kind == "checkin"
    assert json.loads(state_path.read_text())["pending_job_id"] == "checkinjob01"
    assert pending_at is not None
    assert len(deliveries) == 0

    assert _reserve_delivery(runtime, now, "proactive-message") is False
    _deliveries, _pending_at, kind = _load_delivery_state(state_path)
    assert kind == "checkin"
    assert consume_delivery_reservation(
        runtime.hermes_home,
        "cron_checkinjob01_20260101_120000",
        relic_home=tmp_path,
    ) == (True, "checkin")

    assert _reserve_delivery(runtime, now, "proactive-message") is True
    _deliveries, _pending_at, kind = _load_delivery_state(state_path)
    assert kind == "proactivity"
    assert consume_delivery_reservation(
        runtime.hermes_home,
        "cron_proactjob001_20260101_120000",
        relic_home=tmp_path,
    ) == (True, "proactivity")

    assert _reserve_delivery(runtime, now, "diegetic-message") is True
    _deliveries, _pending_at, kind = _load_delivery_state(state_path)
    assert kind == "diegetic"


def test_consume_delivery_reservation_returns_kind_tuple(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    runtime = _CanonicalRuntime(
        scope_id="scope", relic_home=tmp_path, hermes_home=hermes_home, artifacts={}
    )
    _write_message_job_manifest(tmp_path)

    assert consume_delivery_reservation(
        hermes_home, "cron_checkinjob01_20260101_120000", relic_home=tmp_path
    ) == (False, None)

    _reserve_delivery(runtime, None, "checkin-message")
    assert consume_delivery_reservation(
        hermes_home, "cron_otherjob0001_20260101_120000", relic_home=tmp_path
    ) == (False, None)
    assert _load_delivery_state(
        hermes_home / "state" / "checkin_guard.json"
    )[1] is not None
    assert consume_delivery_reservation(
        hermes_home, "cron_checkinjob01_20260101_120000", relic_home=tmp_path
    ) == (True, "checkin")
    assert consume_delivery_reservation(
        hermes_home, "cron_checkinjob01_20260101_120001", relic_home=tmp_path
    ) == (False, None)

    _reserve_delivery(runtime, None, "proactive-message")
    assert consume_delivery_reservation(
        hermes_home, "cron_proactjob001_20260101_120000", relic_home=tmp_path
    ) == (True, "proactivity")


def test_reservation_fails_closed_without_managed_job_manifest(tmp_path: Path) -> None:
    runtime = _CanonicalRuntime(
        scope_id="scope",
        relic_home=tmp_path,
        hermes_home=tmp_path / "hermes",
        artifacts={},
    )

    assert _reserve_delivery(runtime, None, "checkin-message") is False
    assert not (runtime.hermes_home / "state" / "checkin_guard.json").exists()


def test_two_consumers_cannot_claim_one_reservation(tmp_path: Path) -> None:
    runtime = _minimal_runtime(tmp_path)
    _reserve_delivery(runtime, None, "checkin-message")
    barrier = threading.Barrier(3)
    results: list[tuple[bool, str | None]] = []

    def consume() -> None:
        barrier.wait()
        results.append(
            consume_delivery_reservation(
                runtime.hermes_home,
                "cron_checkinjob01_20260101_120000",
                relic_home=tmp_path,
            )
        )

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert sorted(results) == [(False, None), (True, "checkin")]


def test_concurrent_guard_updates_preserve_reservation_delivery_and_reply(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    barrier = threading.Barrier(4)

    actions = (
        lambda: _reserve_delivery(runtime, None, "checkin-message"),
        lambda: record_subject_reply(runtime.hermes_home, now),
        lambda: record_delivery_outcome(runtime.hermes_home, "checkin", now),
    )

    def run(action) -> None:
        barrier.wait()
        action()

    threads = [threading.Thread(target=run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    state_path = runtime.hermes_home / "state" / "checkin_guard.json"
    deliveries, pending_at, kind = _load_delivery_state(state_path)
    streak, last_reply_at = _load_streak_state(state_path)
    assert deliveries == [now]
    assert pending_at is not None
    assert kind == "checkin"
    assert streak == 0
    assert last_reply_at == now


def test_consume_delivery_reservation_expired_returns_false_none(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    state_path = hermes_home / "state" / "checkin_guard.json"
    state_path.parent.mkdir(parents=True)
    stale = datetime.now(timezone.utc) - timedelta(seconds=700)
    state_path.write_text(
        '{"deliveries": [], "pending_at": "' + stale.isoformat() + '", "kind": "checkin"}\n',
        encoding="utf-8",
    )
    _write_message_job_manifest(tmp_path)
    assert consume_delivery_reservation(
        hermes_home, "cron_checkinjob01_20260101_120000", relic_home=tmp_path
    ) == (False, None)


def test_checkin_message_has_no_ask_topic_when_subject_id_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert context["status"] == "ready"
    assert "ask_topic" not in context


def test_checkin_message_drops_raw_ask_topic_when_context_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    monkeypatch.setattr(
        cron_wiring, "_select_ask_decision", lambda *a, **kw: (True, "some_topic_hint")
    )
    monkeypatch.setattr(
        "relic.checkin.context_builder.build_deliver_context",
        lambda *a, **kw: "\n--- ISTRUZIONE MODALITÀ ---\nNessun aggancio concreto.",
    )

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert "ask_topic" not in context
    assert "checkin_context" not in context


def test_checkin_message_has_no_ask_topic_when_decision_says_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    monkeypatch.setattr(cron_wiring, "_select_ask_decision", lambda *a, **kw: (False, None))

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert "ask_topic" not in context

def test_checkin_message_includes_historical_context_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring
    from relic.checkin import context_builder
    from relic.checkin.topic_hint import HEADER

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    monkeypatch.setattr(
        cron_wiring, "_select_ask_decision", lambda *a, **kw: (True, "some_topic_hint")
    )
    monkeypatch.setattr(
        context_builder,
        "build_deliver_context",
        lambda *a, **kw: f"\n{HEADER}\nctx",
    )

    context = _run("checkin-message", tmp_path, scope_id, 12)
    assert context["checkin_context"] == f"\n{HEADER}\nctx"
    assert "ask_topic" not in context

def test_checkin_message_silences_when_topic_is_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring
    from relic.checkin import context_builder

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    monkeypatch.setattr(
        cron_wiring, "_select_ask_decision", lambda *a, **kw: (True, "stale_topic")
    )
    monkeypatch.setattr(
        context_builder,
        "build_deliver_context",
        lambda *a, **kw: "\n--- ISTRUZIONE MODALITÀ ---\nNessun aggancio concreto.",
    )

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert "checkin_context" not in context


def test_checkin_message_drops_raw_topic_when_context_builder_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring
    from relic.checkin import context_builder

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")
    monkeypatch.setattr(
        cron_wiring, "_select_ask_decision", lambda *a, **kw: (True, "raw_internal_hint")
    )
    monkeypatch.setattr(
        context_builder,
        "build_deliver_context",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("builder unavailable")),
    )

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert "ask_topic" not in context
    assert "checkin_context" not in context
def test_checkin_message_fails_open_when_ask_decision_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.gumi_plugin import cron_wiring

    _, scope_id = _runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("RELIC_SUBJECT_ID", "testsubject")

    def _raise(*_args: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise RuntimeError("boom")

    monkeypatch.setattr(cron_wiring, "_select_ask_decision", _raise)

    context = _run("checkin-message", tmp_path, scope_id, 12)

    assert context["status"] == "ready"
    assert "ask_topic" not in context


def test_checkin_blocked_outside_reserved_slots_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the profile reserves morning/evening for check-ins, an afternoon
    check-in is blocked even though the shared delivery window/spacing/cap
    would otherwise allow it."""
    _, scope_id = _runtime(
        tmp_path, monkeypatch, checkin_slots=["morning", "evening"]
    )

    assert _run("checkin-gate", tmp_path, scope_id, 10)["status"] == "eligible"
    assert _run("checkin-gate", tmp_path, scope_id, 15)["status"] == "silent"
    assert _run("checkin-gate", tmp_path, scope_id, 19)["status"] == "eligible"


def test_proactive_and_diegetic_blocked_inside_reserved_checkin_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proactive/diegetic messages only fill the residual (non check-in) slot
    when the profile reserves specific slots for check-ins."""
    _, scope_id = _runtime(
        tmp_path,
        monkeypatch,
        maximum_frequency="3/day",
        checkin_slots=["morning", "evening"],
        diegetic_enabled=True,
    )
    _queue_candidate(tmp_path)

    assert _run("proactive-gate", tmp_path, scope_id, 10)["status"] == "silent"
    assert _run("proactive-gate", tmp_path, scope_id, 15)["status"] == "eligible"
    assert _run("diegetic-gate", tmp_path, scope_id, 19)["status"] == "silent"


def test_morning_diegetic_proactive_evening_cadence_matches_oss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OSS daily rhythm leaves the afternoon for ambient initiatives."""
    _, scope_id = _runtime(
        tmp_path,
        monkeypatch,
        maximum_frequency="4/day",
        checkin_slots=["morning", "evening"],
        diegetic_enabled=True,
    )
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    _queue_candidate(tmp_path)

    # Morning check-in, then the residual afternoon lane, then evening check-in.
    assert _run("checkin-gate", tmp_path, scope_id, 10)["status"] == "eligible"
    record_delivery_outcome(hermes_home, "checkin", datetime(2026, 1, 1, 10, tzinfo=timezone.utc))

    assert _run("diegetic-gate", tmp_path, scope_id, 14)["status"] == "eligible"
    record_delivery_outcome(hermes_home, "diegetic", datetime(2026, 1, 1, 14, tzinfo=timezone.utc))

    assert _run("proactive-gate", tmp_path, scope_id, 17)["status"] == "eligible"
    record_delivery_outcome(hermes_home, "proactivity", datetime(2026, 1, 1, 17, tzinfo=timezone.utc))

    assert _run("checkin-gate", tmp_path, scope_id, 21)["status"] == "eligible"


def test_checkin_slot_reservation_fails_open_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without checkin_slots configured, all actions keep the prior
    (pre-reservation) shared-window behaviour."""
    _, scope_id = _runtime(tmp_path, monkeypatch, maximum_frequency="3/day")
    _queue_candidate(tmp_path)

    assert _run("checkin-gate", tmp_path, scope_id, 15)["status"] == "eligible"
    assert _run("proactive-gate", tmp_path, scope_id, 10)["status"] == "eligible"

def test_record_delivery_outcome_does_not_increment_streak_before_deadline(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"

    record_delivery_outcome(runtime.hermes_home, "checkin")
    streak, _ = _load_streak_state(state_path)
    assert streak == 0

    record_delivery_outcome(runtime.hermes_home, "proactivity")
    streak, _ = _load_streak_state(state_path)
    assert streak == 0


def test_record_delivery_outcome_does_not_increment_streak_for_diegetic(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"

    record_delivery_outcome(runtime.hermes_home, "diegetic")
    streak, _ = _load_streak_state(state_path)
    assert streak == 0


def test_consume_delivery_reservation_alone_does_not_touch_streak(
    tmp_path: Path,
) -> None:
    """Regression: consuming a reservation means a message job was allowed
    to *attempt* delivery, not that content actually reached the subject
    (the LLM may abstain with [SILENT]). The streak must only move via
    `record_delivery_outcome`, once real delivery is confirmed."""
    runtime = _minimal_runtime(tmp_path)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"

    _reserve_delivery(runtime, now, "checkin-message")
    consume_delivery_reservation(
        runtime.hermes_home,
        "cron_checkinjob01_20260101_120000",
        relic_home=tmp_path,
    )
    streak, _ = _load_streak_state(state_path)
    assert streak == 0

    _reserve_delivery(runtime, now, "proactive-message")
    consume_delivery_reservation(
        runtime.hermes_home,
        "cron_proactjob001_20260101_120000",
        relic_home=tmp_path,
    )
    streak, _ = _load_streak_state(state_path)
    assert streak == 0


def test_record_subject_reply_resets_streak_to_zero(tmp_path: Path) -> None:
    runtime = _minimal_runtime(tmp_path)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"

    _write_streak_state(state_path, 3, None)
    streak, _ = _load_streak_state(state_path)
    assert streak == 3

    record_subject_reply(runtime.hermes_home, now)
    streak, last_reply_at = _load_streak_state(state_path)
    assert streak == 0
    assert last_reply_at == now


def test_non_response_backoff_blocks_checkin_and_proactive_not_diegetic(
    tmp_path: Path,
) -> None:
    runtime = _minimal_runtime(tmp_path)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"
    _write_streak_state(state_path, 3, None)

    assert _non_response_backoff_blocks("checkin-gate", runtime) is True
    assert _non_response_backoff_blocks("checkin-message", runtime) is True
    assert _non_response_backoff_blocks("proactive-gate", runtime) is True
    assert _non_response_backoff_blocks("proactive-message", runtime) is True
    assert _non_response_backoff_blocks("diegetic-gate", runtime) is False
    assert _non_response_backoff_blocks("diegetic-message", runtime) is False


def test_non_response_backoff_below_threshold_does_not_block(tmp_path: Path) -> None:
    runtime = _minimal_runtime(tmp_path)
    state_path = runtime.hermes_home / "state" / "checkin_guard.json"
    _write_streak_state(state_path, 2, None)

    assert _non_response_backoff_blocks("checkin-gate", runtime) is False


def test_checkin_gate_respects_non_response_backoff_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full-path check: a streak at threshold silences checkin-gate; a
    recorded subject reply immediately lifts the backoff."""
    _, scope_id = _runtime(tmp_path, monkeypatch, maximum_frequency="10/day")
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    state_path = hermes_home / "state" / "checkin_guard.json"

    _write_streak_state(state_path, 3, None)
    assert _run("checkin-gate", tmp_path, scope_id, 10)["status"] == "silent"

    record_subject_reply(hermes_home, datetime(2026, 1, 1, 9, tzinfo=timezone.utc))
    assert _run("checkin-gate", tmp_path, scope_id, 10)["status"] == "eligible"


def test_reservation_alone_does_not_consume_daily_cap_only_confirmed_delivery_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a *-message job reserves the slot (anti-race lock) before
    the LLM decides what to write. If the LLM abstains with [SILENT], no
    real message ever reaches the subject, so it must NOT consume the
    shared daily quota (`maximum_frequency`) — otherwise a later, genuinely
    eligible check-in gets silently blocked for the rest of the day even
    though nothing was ever actually sent."""
    _, scope_id = _runtime(
        tmp_path, monkeypatch, maximum_frequency="1/day", setup_mode="legacy_import"
    )
    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    _queue_candidate(tmp_path)

    # 10:00 — proactive-message reserves the slot ...
    first = _run("proactive-message", tmp_path, scope_id, 10)
    assert first["status"] == "ready"

    # ... the LLM abstains ([SILENT]): nothing was delivered, so the
    # shared daily cap must still be open for a later attempt today.
    assert consume_delivery_reservation(
        hermes_home,
        "cron_proactjob001_20260101_100000",
        relic_home=tmp_path,
    ) == (True, "proactivity")
    second = _run("checkin-message", tmp_path, scope_id, 15)
    assert second["status"] == "ready"

    # Once a delivery is actually confirmed, the shared cap applies.
    record_delivery_outcome(
        hermes_home, "checkin", datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
    )
    third = _run("proactive-message", tmp_path, scope_id, 16)
    assert third["status"] == "silent"


def test_no_spectrum_facet_descriptions_carry_no_clinical_jargon_or_dev_notes() -> None:
    """Regression for the past incident: meta_cognition.cognitive_biases'
    description was 'Bias cognitivi osservati (non spettro, lista di bias)' —
    build_question_hint() returns a no-spectrum facet's description verbatim as
    the topic hint (no spectrum to combine it with), so the raw word 'bias' and
    the internal '(non spettro...)' dev annotation both reached the model and
    leaked into the delivered check-in question."""
    from relic.checkin.db_init import FACETS

    no_spectrum = [f for f in FACETS if f["spectrum_low"] is None]
    assert len(no_spectrum) == 3  # aesthetic.music_taste, meta_cognition.cognitive_biases, values.core_values

    for facet in no_spectrum:
        desc = facet["description"]
        assert "(" not in desc and ")" not in desc, facet["id"]
        assert "bias" not in desc.lower(), facet["id"]
