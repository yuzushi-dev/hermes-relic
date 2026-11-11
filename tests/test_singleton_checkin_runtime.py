from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME
import pytest


import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relic.hermes_plugin.memory_provider import RelicMemoryProvider
from relic.profile.registry import ProfileRegistry
from relic.profile.user_runtime import dispatch_action


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _runtime(tmp_path: Path, monkeypatch):
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
    workspace = registry.hermes_profiles_home / HERMES_USER_PROFILE_NAME / "workspace" / "gumi"
    workspace.mkdir(parents=True, exist_ok=True)
    for name in (
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    ):
        (workspace / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILES_HOME", str(registry.hermes_profiles_home))
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)
    managed_cron = tmp_path / "user" / "managed_cron.json"
    managed_cron.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {
                    "gumi_checkin_message": {
                        "id": "checkinjob01",
                        "fingerprint": "test",
                    },
                    "gumi_proactive_message": {
                        "id": "proactjob001",
                        "fingerprint": "test",
                    },
                    "gumi_diegetic_message": {
                        "id": "diegetic001",
                        "fingerprint": "test",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return registry, user


def _enable_checkin(store: Path) -> None:
    policy = store / "user" / "checkin" / "delivery_policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        json.dumps(
            {
                "consent_for_active_elicitation": True,
                "consent_for_passive_extraction": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy.chmod(0o600)


def test_feature_builder_reads_explicit_singleton_store(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-compaction",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    checkin_dir = tmp_path / "user" / "checkin"
    with sqlite3.connect(checkin_dir / "relic.db") as conn:
        conn.execute(
            """INSERT INTO checkin_cadence_state
               (subject_id, non_response_streak, updated_at)
               VALUES (?, ?, ?)""",
            (user.runtime_scope_id, 2, NOW.isoformat()),
        )
        conn.commit()

    from relic.checkin.features import build_checkin_features

    features = build_checkin_features(
        subject_id=user.runtime_scope_id,
        decision_type="checkin",
        relic_home=tmp_path,
        hermes_home=tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME,
        now=NOW,
        subject_dir=checkin_dir,
    )

    assert features.non_response_streak == 2


def test_feature_builder_reads_onboarding_battery_calibration(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-compaction",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    checkin_dir = tmp_path / "user" / "checkin"
    baseline_path = checkin_dir / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["battery"] = {
        "scores": {
            "project_calibration": {
                "fictional_diegesis_tolerance": 1.0,
                "comfort_with_initiative": 0.8,
                "continuity_preference": 0.7,
            }
        }
    }
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    from relic.checkin.features import build_checkin_features

    features = build_checkin_features(
        subject_id=user.runtime_scope_id,
        decision_type="diegetic",
        relic_home=tmp_path,
        hermes_home=tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME,
        now=NOW,
        subject_dir=checkin_dir,
        baseline_path=baseline_path,
    )

    assert features.diegetic_tolerance == 1.0
    assert features.comfort_with_initiative == 0.8
    assert features.continuity_preference == 0.7


def test_singleton_gate_uses_reference_policy(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    calls: dict[str, object] = {}

    from relic.checkin.policy import CheckinFeatures, Decision, EventType, Posture

    def build_features(**kwargs):
        calls["features"] = kwargs
        return CheckinFeatures(subject_id=user.runtime_scope_id, consent_active=True)

    def select(features, *, decision_type, policy_enabled, reflection_enabled=False):
        calls["decision"] = (features, decision_type, policy_enabled, reflection_enabled)
        return Decision(EventType.CHECKIN, Posture.ASK, "reference-policy")

    monkeypatch.setattr("relic.checkin.features.build_checkin_features", build_features)
    monkeypatch.setattr("relic.checkin.policy.select_decision", select)

    result = dispatch_action(
        "checkin-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW.replace(hour=14),
    )

    assert result["status"] == "eligible"
    assert calls["features"]["subject_dir"] == tmp_path / "user" / "checkin"
    assert calls["decision"][1:] == ("checkin", True, False)


def test_diegetic_message_uses_posture_selected_by_policy(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    captured: dict[str, str] = {}

    from relic.checkin.policy import CheckinFeatures, Decision, EventType, Posture

    monkeypatch.setattr(
        "relic.checkin.features.build_checkin_features",
        lambda **_kwargs: CheckinFeatures(
            subject_id=user.runtime_scope_id,
            consent_active=True,
        ),
    )
    monkeypatch.setattr(
        "relic.checkin.policy.select_decision",
        lambda *_args, **_kwargs: Decision(
            EventType.DIEGETIC,
            Posture.SMALL_SHARE,
            "diegetic_share_factual",
        ),
    )
    monkeypatch.setattr(
        "relic.checkin.features.persist_features",
        lambda *_args, **_kwargs: None,
    )

    def fake_initiative_context(action, runtime, now, *, posture):
        captured["posture"] = posture
        return "delivery-context", "text"

    monkeypatch.setattr(
        "relic.profile.user_runtime._initiative_context", fake_initiative_context
    )

    result = dispatch_action(
        "diegetic-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )

    assert result["status"] == "ready"
    assert result["posture"] == "small_share"
    assert result["policy_reason"] == "diegetic_share_factual"
    assert result["policy_reason"] not in result["delivery_context"]
    assert captured == {"posture": "small_share"}


def test_dispatch_matches_reference_policy_outside_delivery_window(
    tmp_path, monkeypatch
) -> None:
    from relic.checkin.policy import CheckinFeatures, EventType, select_decision

    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    reference = select_decision(
        CheckinFeatures(
            subject_id=user.runtime_scope_id,
            consent_active=True,
            delivery_window_open=False,
            facet_status="ask_now",
            topic_freshness=1.0,
        ),
        decision_type="checkin",
        policy_enabled=True,
    )
    dispatched = dispatch_action(
        "checkin-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW.replace(hour=14),
    )

    assert reference.event_type is EventType.SILENT
    assert dispatched["status"] == reference.event_type.value


def test_proactive_reengage_floor_is_not_blocked_by_legacy_guard(
    tmp_path, monkeypatch
) -> None:
    registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    profile_path = tmp_path / "user" / "gumi_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["setup_mode"] = "legacy_import"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    dispatch_action(
        "continuity-compaction",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    with sqlite3.connect(tmp_path / "user" / "checkin" / "relic.db") as conn:
        conn.execute(
            """INSERT INTO checkin_cadence_state
               (subject_id, non_response_streak, last_delivered_initiative_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (
                user.runtime_scope_id,
                3,
                (NOW - timedelta(days=5)).isoformat(),
                NOW.isoformat(),
            ),
        )
        conn.commit()

    result = dispatch_action(
        "proactive-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )

    assert result["status"] == "eligible"
    assert result["wakeAgent"] is True


@pytest.mark.parametrize(
    ("action", "policy_text"),
    (
        ("proactive-gate", '{"consent_for_active_elicitation": false}'),
        ("diegetic-gate", '{"consent_for_active_elicitation": false}'),
        ("proactive-gate", "not-json"),
        ("diegetic-gate", "not-json"),
    ),
)
def test_all_initiatives_fail_closed_without_active_consent(
    tmp_path, monkeypatch, action: str, policy_text: str
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    policy = tmp_path / "user" / "checkin" / "delivery_policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(policy_text + "\n", encoding="utf-8")

    result = dispatch_action(
        action,
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )

    assert result["status"] == "silent"
    assert result["wakeAgent"] is False


def test_canonical_delivery_becomes_non_response_only_after_24_hours(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-compaction",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    from relic.checkin.outcome_reconciler import reconcile_due_outcomes
    from relic.profile.user_runtime import record_delivery_outcome

    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    db_path = tmp_path / "user" / "checkin" / "relic.db"
    record_delivery_outcome(
        hermes_home,
        "checkin",
        NOW,
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT non_response_streak FROM checkin_cadence_state"
        ).fetchone() == (0,)

    assert reconcile_due_outcomes(
        user.runtime_scope_id,
        relic_home=tmp_path,
        hermes_home=hermes_home,
        now=NOW.replace(day=2, hour=13),
        db_path=db_path,
    ) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT non_response_streak FROM checkin_cadence_state"
        ).fetchone() == (1,)


def test_outcome_reconcile_recovers_after_log_append_crash_without_double_count(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-compaction",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    from relic.checkin import outcome_reconciler
    from relic.profile.user_runtime import record_delivery_outcome

    hermes_home = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME
    db_path = tmp_path / "user" / "checkin" / "relic.db"
    record_delivery_outcome(
        hermes_home,
        "checkin",
        NOW,
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
    )
    append_event = outcome_reconciler._append_event
    monkeypatch.setattr(
        outcome_reconciler,
        "_append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")),
    )

    with pytest.raises(OSError, match="crash"):
        outcome_reconciler.reconcile_due_outcomes(
            user.runtime_scope_id,
            relic_home=tmp_path,
            hermes_home=hermes_home,
            now=NOW + timedelta(hours=25),
            db_path=db_path,
        )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT non_response_streak FROM checkin_cadence_state"
        ).fetchone() == (1,)

    monkeypatch.setattr(outcome_reconciler, "_append_event", append_event)
    assert outcome_reconciler.reconcile_due_outcomes(
        user.runtime_scope_id,
        relic_home=tmp_path,
        hermes_home=hermes_home,
        now=NOW + timedelta(hours=25),
        db_path=db_path,
    ) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT non_response_streak FROM checkin_cadence_state"
        ).fetchone() == (1,)


def test_subject_message_metadata_excludes_cron_scaffold(tmp_path) -> None:
    from relic.checkin.hermes_state_reader import last_subject_msg_at, subject_avg_tokens

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    genuine = NOW.replace(day=1, hour=8)
    scaffold = NOW.replace(day=2, hour=12)
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute(
            "CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)"
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            [
                (genuine.timestamp(), "messaggio reale"),
                (
                    scaffold.timestamp(),
                    "[IMPORTANT: You are running as a scheduled cron job.] prompt",
                ),
            ],
        )
        conn.commit()

    assert last_subject_msg_at(hermes_home) == genuine
    assert subject_avg_tokens(hermes_home, genuine.replace(hour=7)) == pytest.approx(
        len("messaggio reale") / 4.0
    )


def test_context_sections_use_replay_clock_for_cutoffs(tmp_path) -> None:
    from relic.checkin.context_builder import (
        build_observations_section,
        build_recent_subject_messages_section,
    )

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            ((NOW - timedelta(hours=1)).timestamp(), "messaggio nel replay"),
        )

    db_path = tmp_path / "relic.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE observations "
            "(content TEXT, source_type TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO observations VALUES (?, 'checkin_reply', ?)",
            ("osservazione nel replay", (NOW - timedelta(days=1)).isoformat()),
        )

    assert "messaggio nel replay" in build_recent_subject_messages_section(
        hermes_home, now=NOW
    )
    assert "osservazione nel replay" in build_observations_section(db_path, now=NOW)


def test_recent_subject_messages_strips_a_long_replied_to_quote(tmp_path) -> None:
    """Regression for the past incident: replying to a 285-char Gumi
    message left the 250-char bracket-search cap unable to find the closing
    ']', so the raw quote (Gumi's own words) leaked into the checkin context
    as if it were the subject's reply — Gumi then asked a follow-up about its
    own story instead of the subject's actual answer."""
    from relic.checkin.context_builder import build_recent_subject_messages_section

    long_quote = (
        "Ho passato un bel po' a provare quel nuovo esercizio stasera, "
        "e c'è una soddisfazione strana quando riesco a farlo uscire pulito ✨\n"
        "C'è stato oggi qualcosa di piccolo che ti ha dato una bella carica, "
        "tipo una piccola vittoria, o forse solo un momento tranquillo e piacevole? 💚"
    )
    assert len(long_quote) > 250
    real_reply = "Sì! Ieri sera finalmente il progetto, dopo settimane"
    content = f'[Replying to: "{long_quote}"] {real_reply}'

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            ((NOW - timedelta(hours=1)).timestamp(), content),
        )

    section = build_recent_subject_messages_section(hermes_home, now=NOW)

    assert real_reply in section
    assert "[Replying to:" not in section


def test_strip_reply_quote_prefix_handles_a_quote_over_250_chars() -> None:
    """Same root cause as above, in reply_capture's own copy of the strip
    logic (its docstring already noted it 'mirrors context_builder')."""
    from relic.checkin.reply_capture import strip_reply_quote_prefix

    long_quote = (
        "Ho passato un bel po' a provare quel nuovo esercizio stasera, "
        "e c'è una soddisfazione strana quando riesco a farlo uscire pulito ✨\n"
        "C'è stato oggi qualcosa di piccolo che ti ha dato una bella carica, "
        "tipo una piccola vittoria, o forse solo un momento tranquillo e piacevole? 💚"
    )
    assert len(long_quote) > 250
    real_reply = "Sì! Ieri sera finalmente il progetto, dopo settimane"

    cleaned = strip_reply_quote_prefix(f'[Replying to: "{long_quote}"] {real_reply}')

    assert cleaned == real_reply


def test_proactive_context_keeps_anchor_from_four_days_ago(tmp_path) -> None:
    from relic.checkin.context_builder import (
        build_deliver_context,
        build_recent_subject_messages_section,
    )

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            ((NOW - timedelta(days=4)).timestamp(), "il filo concreto di quattro giorni fa"),
        )

    assert "filo concreto" not in build_recent_subject_messages_section(
        hermes_home, now=NOW
    )
    context = build_deliver_context(
        "scope-1",
        hermes_home,
        tmp_path,
        event_type="proactive",
        posture="brief_share",
        persist_topic_hint=False,
        media_type="text",
        now=NOW,
    )

    assert "filo concreto di quattro giorni fa" in context


def test_proactive_anchor_window_has_a_seven_day_boundary(tmp_path) -> None:
    from relic.checkin.context_builder import build_recent_subject_messages_section

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            ((NOW - timedelta(days=7)).timestamp(), "ancora al confine"),
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)",
            (
                (NOW - timedelta(days=7, seconds=1)).timestamp(),
                "ancora oltre il confine",
            ),
        )

    section = build_recent_subject_messages_section(
        hermes_home,
        hours=7 * 24,
        now=NOW,
    )

    assert "ancora al confine" in section
    assert "ancora oltre il confine" not in section


def test_topic_builder_falls_back_when_first_ranked_hint_repeats(
    tmp_path, monkeypatch
) -> None:
    from relic.checkin.context_builder import build_topic_hint_section
    from relic.checkin.db_init import init_db, seed_facets
    from relic.checkin.topic_hint import HEADER

    db_path = tmp_path / "relic.db"
    with init_db(db_path) as conn:
        seed_facets(conn)
        conn.execute(
            """INSERT INTO checkin_exchanges
               (facet_id, question_text, reply_text, asked_at)
               VALUES (?, ?, ?, ?)""",
            (
                "cognitive.decision_speed",
                "stesso argomento concreto",
                "risposta",
                "2025-01-01T10:00:00+00:00",
            ),
        )
        conn.commit()

    selections = iter(
        [
            {
                "status": "ask_now",
                "selected_facet": "cognitive.decision_speed",
                "question_hint": "stesso argomento concreto",
            },
            {
                "status": "ask_now",
                "selected_facet": "cognitive.risk_tolerance",
                "question_hint": "un tema davvero nuovo",
            },
        ]
    )
    monkeypatch.setattr(
        "relic.checkin.question_engine.select_facet",
        lambda *_args, **_kwargs: next(selections),
    )

    section = build_topic_hint_section(
        "scope",
        db_path,
        tmp_path / "baseline.json",
        persist_exchange=False,
        now=NOW,
    )

    assert section == f"\n{HEADER}\nun tema davvero nuovo"


def test_checkin_gate_and_context_share_anti_repeat_topic(tmp_path, monkeypatch) -> None:
    from relic.checkin.db_init import init_db, seed_facets
    from relic.profile.user_runtime import (
        _build_checkin_context,
        _checkin_ask_topic,
        _load_runtime,
    )

    registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    db_path = tmp_path / "user" / "checkin" / "relic.db"
    with init_db(db_path) as conn:
        seed_facets(conn)
        conn.execute(
            "INSERT INTO checkin_exchanges "
            "(facet_id, question_text, reply_text, asked_at) VALUES (?, ?, ?, ?)",
            (
                "cognitive.decision_speed",
                "stesso argomento concreto",
                "risposta",
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )
        conn.commit()

    calls = 0

    def fake_select(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls % 2:
            return {
                "status": "ask_now",
                "selected_facet": "cognitive.decision_speed",
                "question_hint": "stesso argomento concreto",
            }
        return {
            "status": "ask_now",
            "selected_facet": "cognitive.risk_tolerance",
            "question_hint": "un tema davvero nuovo",
        }

    monkeypatch.setattr("relic.checkin.question_engine.select_facet", fake_select)
    runtime = _load_runtime(tmp_path, user.runtime_scope_id)
    topic = _checkin_ask_topic(runtime, NOW)
    context = _build_checkin_context(runtime, NOW, topic)

    assert topic == "un tema davvero nuovo"
    assert "un tema davvero nuovo" in (context or "")


def test_checkin_question_uses_private_user_store_without_subject_env(tmp_path, monkeypatch) -> None:
    registry, user = _runtime(tmp_path, monkeypatch)
    result = dispatch_action(
        "checkin-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert result["status"] == "silent"
    assert "checkin_context" not in result

    _enable_checkin(tmp_path)
    result = dispatch_action(
        "checkin-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert result["status"] == "ready"
    assert "checkin_context" in result
    assert "spunto di conversazione" in result["checkin_context"].lower()

    db_path = tmp_path / "user" / "checkin" / "relic.db"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM facets").fetchone()[0] == 60
        assert conn.execute("SELECT count(*) FROM checkin_exchanges").fetchone()[0] == 1


def test_migrated_legacy_store_drives_unmocked_checkin_policy_and_context(
    tmp_path, monkeypatch
) -> None:
    from relic.checkin.db_init import init_db, seed_facets
    from relic.profile.migration import migrate_legacy_checkin_store

    target_home = tmp_path / "target"
    _registry, user = _runtime(target_home, monkeypatch)
    source = tmp_path / "legacy" / "testsubject"
    source.mkdir(parents=True)
    (source / "subject_profile.json").write_text(
        '{"subject_id": "testsubject"}\n', encoding="utf-8"
    )
    with init_db(source / "relic.db") as conn:
        seed_facets(conn)
        conn.execute(
            "INSERT INTO observations "
            "(facet_id, source_type, content, created_at) VALUES (?, ?, ?, ?)",
            (
                "cognitive.decision_speed",
                "checkin_reply",
                "preferisce decidere con calma",
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )
        conn.commit()
    (source / "delivery_policy.json").write_text(
        '{"consent_for_active_elicitation": true, '
        '"consent_for_passive_extraction": true}\n',
        encoding="utf-8",
    )

    migrated = migrate_legacy_checkin_store(
        source,
        target_relic_home=target_home,
    )
    result = dispatch_action(
        "checkin-message",
        relic_home=target_home,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW.replace(hour=11, minute=30),
    )

    assert migrated.imported is True
    assert result["status"] == "ready"
    assert result["wakeAgent"] is True
    assert "checkin_context" in result
    assert "spunto di conversazione" in result["checkin_context"].lower()
    with sqlite3.connect(target_home / "user" / "checkin" / "relic.db") as conn:
        assert conn.execute("SELECT count(*) FROM observations").fetchone() == (1,)


def test_continuity_sync_processes_singleton_facets_without_subject_env(tmp_path, monkeypatch) -> None:
    registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-sync",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    state_db = registry.hermes_user_home / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.commit()

    calls: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(
        "relic.gumi_plugin.memory_sync.sync",
        lambda _home: {"errors": 0, "appended": 0},
    )
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_passive_extraction",
        lambda *args, **kwargs: {"processed": 0},
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges",
        lambda conn, baseline, subject, **kwargs: calls.append(
            (
                Path(conn.execute("PRAGMA database_list").fetchone()[2]),
                Path(baseline),
                subject,
            )
        ),
    )

    dispatch_action(
        "continuity-sync",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert calls
    assert calls[0][1] == tmp_path / "user" / "checkin" / "baseline.json"
    assert calls[0][2] == user.runtime_scope_id


def test_foreground_memory_provider_captures_consented_user_reply(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    current = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    dispatch_action(
        "checkin-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=current,
    )

    provider = RelicMemoryProvider(
        runtime_scope_id=user.runtime_scope_id,
        relic_home=str(tmp_path),
    )
    provider.sync_turn("La risposta concreta", "Va bene")

    with sqlite3.connect(tmp_path / "user" / "checkin" / "relic.db") as conn:
        assert conn.execute("SELECT reply_text FROM checkin_exchanges").fetchone()[0] == "La risposta concreta"


def test_proactive_queue_preserves_suggested_posture(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    captured: dict[str, str] = {}

    def fake_initiative_context(action, runtime, now, *, posture):
        captured["posture"] = posture
        return "delivery-context", "text"

    monkeypatch.setattr(
        "relic.profile.user_runtime._initiative_context", fake_initiative_context
    )
    dispatch_action(
        "proactive-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    queue = tmp_path / "user" / "checkin" / "proactive_queue.jsonl"
    queue.write_text(
        json.dumps(
            {
                "id": "candidate-1",
                "priority": 0.9,
                "signal_ref": "recent-thread",
                "suggested_posture": "small_share",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = dispatch_action(
        "proactive-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert result["status"] == "ready"
    assert captured["posture"] == "small_share"
    assert result["proactive_signal_ref"] == "recent-thread"


def test_proactive_candidate_is_not_consumed_when_reservation_is_occupied(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    assert dispatch_action(
        "checkin-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )["status"] == "ready"
    queue = tmp_path / "user" / "checkin" / "proactive_queue.jsonl"
    queue.write_text(
        '{"id":"candidate","priority":0.9,"signal_ref":"test"}\n',
        encoding="utf-8",
    )

    result = dispatch_action(
        "proactive-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )

    assert result["status"] == "silent"
    assert "consumed_at" not in json.loads(queue.read_text(encoding="utf-8"))


def test_proactive_dispatch_threads_replay_clock_into_context(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    captured: dict[str, datetime | None] = {}

    def build_context(*_args, **kwargs):
        captured["now"] = kwargs.get("now")
        return "replay context"

    monkeypatch.setattr(
        "relic.checkin.context_builder.build_deliver_context", build_context
    )
    queue = tmp_path / "user" / "checkin" / "proactive_queue.jsonl"
    queue.write_text(
        '{"id":"candidate","priority":0.9,"signal_ref":"test"}\n',
        encoding="utf-8",
    )

    result = dispatch_action(
        "proactive-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )

    assert result["status"] == "ready"
    assert captured == {"now": NOW}
    assert json.loads(queue.read_text(encoding="utf-8"))["consumed_at"]



def test_gate_context_emits_wake_agent_false_when_silent(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    result = dispatch_action(
        "checkin-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW.replace(hour=23),
    )
    assert result["status"] == "silent"
    assert result["wakeAgent"] is False


def test_media_selector_honors_cooldown_for_single_enabled_media(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    _enable_checkin(tmp_path)
    profile_path = tmp_path / "user" / "gumi_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["media"]["images"] = {"selected": True, "enabled": True}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(
        "relic.gumi_plugin.media_state.is_media_eligible",
        lambda *_args, **_kwargs: False,
    )

    from relic.shared_continuity.service import enqueue_proactive_candidate

    enqueue_proactive_candidate(
        subject_id=user.runtime_scope_id,
        signal_ref="media-test",
        priority=0.9,
        relic_home=str(tmp_path),
    )

    from relic.profile.user_runtime import _media_type

    runtime = dispatch_action(
        "proactive-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert runtime["gate"].splitlines()[1] == "tipo: text"


def test_diegetic_brief_share_profile_disables_topic_hint() -> None:
    from relic.checkin.context_builder import _resolve_section_flags

    flags = _resolve_section_flags("diegetic", "brief_share")
    assert flags["topic_hint"] is False


def test_diegetic_small_share_profile_excludes_sensitive_context() -> None:
    from relic.checkin.context_builder import _resolve_section_flags

    flags = _resolve_section_flags("diegetic", "small_share")

    assert flags["recent_checkins"] is False
    assert flags["recent_subject_messages"] is False
    assert flags["observations"] is False
    assert flags["topic_hint"] is False
    assert flags["last_exchange"] is False


def test_proactive_small_share_profile_keeps_only_anchor_and_style() -> None:
    from relic.checkin.context_builder import _resolve_section_flags

    flags = _resolve_section_flags("proactive", "small_share")

    assert flags["recent_subject_messages"] is True
    assert flags["style_hints"] is True
    assert flags["recent_checkins"] is False
    assert flags["observations"] is False
    assert flags["topic_hint"] is False
    assert flags["last_exchange"] is False


@pytest.mark.parametrize("event_type", ["proactive", "diegetic"])
def test_unknown_initiative_posture_fails_closed(event_type: str) -> None:
    from relic.checkin.context_builder import _resolve_section_flags

    flags = _resolve_section_flags(event_type, "unknown")

    assert flags == {
        "recent_checkins": False,
        "recent_subject_messages": False,
        "observations": False,
        "topic_hint": False,
        "style_hints": False,
        "avatar": True,
        "last_exchange": False,
    }


def test_empty_anchor_fallback_has_no_copyable_notes_example(tmp_path) -> None:
    from relic.checkin.context_builder import build_deliver_context

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    context = build_deliver_context(
        "scope-1",
        hermes_home,
        tmp_path,
        event_type="diegetic",
        posture="brief_share",
        persist_topic_hint=False,
        media_type="text",
        now=NOW,
    )

    assert "appunti del weekend" not in context
    assert "non copiare" in context.lower()


def test_proactive_producer_uses_canonical_queue_when_scope_is_active(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    from relic.shared_continuity.service import enqueue_proactive_candidate

    enqueue_proactive_candidate(
        subject_id=user.runtime_scope_id,
        signal_ref="producer-test",
        relic_home=str(tmp_path),
    )
    assert (tmp_path / "user" / "checkin" / "proactive_queue.jsonl").exists()
    assert not (
        tmp_path / "subjects" / user.runtime_scope_id / "proactive_queue.jsonl"
    ).exists()

@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("configured_via_env", [False, True])
def test_symlinked_relic_home_is_trusted_by_producer_and_consumer(
    tmp_path, monkeypatch, canonical, configured_via_env
) -> None:
    from relic.gumi_plugin.proactive_consumer import consume_one
    from relic.hermes_runtime import RuntimeDecision
    from relic.shared_continuity.service import enqueue_proactive_candidate

    actual_home = tmp_path / "actual-relic"
    home = tmp_path / "relic"
    actual_home.mkdir()
    home.symlink_to(actual_home, target_is_directory=True)
    if configured_via_env:
        monkeypatch.setenv("RELIC_HOME", str(home))
        configured_home = None
    else:
        monkeypatch.delenv("RELIC_HOME", raising=False)
        configured_home = home
    if canonical:
        monkeypatch.setenv("RELIC_RUNTIME_SCOPE_ID", "scope-1")
        expected_queue = actual_home / "user" / "checkin" / "proactive_queue.jsonl"
    else:
        monkeypatch.delenv("RELIC_RUNTIME_SCOPE_ID", raising=False)
        expected_queue = actual_home / "subjects" / "scope-1" / "proactive_queue.jsonl"

    enqueue_proactive_candidate(
        subject_id="scope-1",
        signal_ref="trusted-home",
        priority=0.9,
        relic_home=str(configured_home) if configured_home is not None else None,
    )
    decision, _reasons, candidate = consume_one(
        "scope-1",
        tmp_path / "hermes",
        configured_home,
        now=NOW,
    )

    assert expected_queue.exists()
    assert decision is RuntimeDecision.DELIVER
    assert candidate["proactive_signal_ref"] == "trusted-home"


def test_proactive_queue_rejects_nested_symlink_below_trusted_home(
    tmp_path, monkeypatch
) -> None:
    from relic.gumi_plugin.proactive_consumer import consume_one
    from relic.shared_continuity.service import enqueue_proactive_candidate

    actual_home = tmp_path / "actual-relic"
    home = tmp_path / "relic"
    external = tmp_path / "external"
    actual_home.mkdir()
    external.mkdir()
    home.symlink_to(actual_home, target_is_directory=True)
    (actual_home / "user").mkdir()
    (actual_home / "user" / "checkin").symlink_to(
        external, target_is_directory=True
    )
    monkeypatch.setenv("RELIC_RUNTIME_SCOPE_ID", "scope-1")

    with pytest.raises(ValueError, match="proactive queue is invalid"):
        enqueue_proactive_candidate(
            subject_id="scope-1",
            signal_ref="nested-symlink",
            relic_home=str(home),
        )
    with pytest.raises(ValueError, match="proactive queue is invalid"):
        consume_one("scope-1", tmp_path / "hermes", home, now=NOW)


def test_untrusted_queue_path_rejects_symlinked_home(tmp_path) -> None:
    from relic.gumi_plugin.proactive_consumer import load_candidates

    actual_home = tmp_path / "actual-relic"
    home = tmp_path / "relic"
    actual_home.mkdir()
    home.symlink_to(actual_home, target_is_directory=True)

    with pytest.raises(ValueError, match="proactive queue is invalid"):
        load_candidates(home / "subjects" / "scope-1" / "proactive_queue.jsonl")


def test_facet_sync_receives_durable_user_continuity_store(tmp_path, monkeypatch) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    dispatch_action(
        "continuity-sync",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    state_db = tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("CREATE TABLE messages (timestamp REAL, role TEXT, content TEXT)")
        conn.commit()

    captured: list[object] = []
    monkeypatch.setattr(
        "relic.gumi_plugin.memory_sync.sync",
        lambda _home: {"errors": 0, "appended": 0},
    )
    monkeypatch.setattr(
        "relic.checkin.passive_extractor.run_passive_extraction",
        lambda *args, **kwargs: {"processed": 0},
    )
    monkeypatch.setattr(
        "relic.checkin.facet_updater.process_pending_exchanges",
        lambda *args, **kwargs: captured.append(kwargs.get("continuity_store")),
    )
    dispatch_action(
        "continuity-sync",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert captured
    assert captured[0].__class__.__name__ == "RuntimeContinuityStore"


def test_proactive_no_candidate_does_not_wake_or_leave_delivery_reservation(
    tmp_path, monkeypatch
) -> None:
    _registry, user = _runtime(tmp_path, monkeypatch)
    result = dispatch_action(
        "proactive-message",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=NOW,
    )
    assert result["status"] == "silent"
    assert result["wakeAgent"] is False
    from relic.profile.user_runtime import _delivery_state_path, _load_delivery_state

    _deliveries, pending, kind = _load_delivery_state(
        _delivery_state_path(tmp_path / "hermes-profiles" / HERMES_USER_PROFILE_NAME)
    )
    assert pending is None
    assert kind is None


def test_strong_facet_promotion_persists_unconfirmed_user_candidate(tmp_path) -> None:
    from relic.checkin.facet_updater import (
        ExtractionResult,
        _promote_observation_to_marker,
    )
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    store = RuntimeContinuityStore(tmp_path)
    extraction = ExtractionResult(
        facet_id="relational.loyalty_pattern",
        exchange_id=1,
        informative=True,
        signal_position=0.8,
        signal_strength=0.9,
        observation_summary="preferisce mantenere promesse",
        confidence_delta=0.2,
    )
    _promote_observation_to_marker(
        extraction,
        "scope-1",
        continuity_store=store,
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
    )
    _promote_observation_to_marker(
        extraction,
        "scope-1",
        continuity_store=store,
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
    )

    data = json.loads(
        (tmp_path / "user" / "continuity.json").read_text(encoding="utf-8")
    )
    assert len(data["markers"]) == 1
    assert data["markers"][0]["candidate_for_confirmation"] is True
    assert data["markers"][0]["user_confirmation"] is False
def test_proactive_producer_rejects_symlinked_canonical_queue_parent(tmp_path, monkeypatch):
    from relic.shared_continuity.service import enqueue_proactive_candidate

    home = tmp_path / "relic"
    external = tmp_path / "external"
    (home / "user").mkdir(parents=True)
    external.mkdir()
    (home / "user" / "checkin").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("RELIC_RUNTIME_SCOPE_ID", "scope-1")

    with pytest.raises(ValueError, match="proactive queue is invalid"):
        enqueue_proactive_candidate(
            subject_id="scope-1",
            signal_ref="symlink-test",
            relic_home=str(home),
        )
def test_candidate_confirmation_promotes_only_matching_scope(tmp_path):
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    store = RuntimeContinuityStore(tmp_path)
    marker = store.propose_candidate(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["preferisce", "mantenere", "promesse"],
    )

    assert (
        store.confirm_candidate(
            marker_id=marker["marker_id"],
            runtime_scope_id="scope-2",
            hermes_profile_id=HERMES_USER_PROFILE_NAME,
        )
        is None
    )
    confirmed = store.confirm_candidate(
        marker_id=marker["marker_id"],
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["preferisco", "mantenere", "promesse"],
    )

    assert confirmed is not None
    assert confirmed["user_confirmation"] is True
    assert confirmed["candidate_for_confirmation"] is False
    assert confirmed["source_type"] == "subject_confirmed"
    assert store.get_recent_candidates(
        runtime_scope_id="scope-1", hermes_profile_id=HERMES_USER_PROFILE_NAME
    ) == []
    assert store.get_recent_markers(
        runtime_scope_id="scope-1", hermes_profile_id=HERMES_USER_PROFILE_NAME
    )[0]["user_words"] == ["preferisco", "mantenere", "promesse"]


def test_memory_provider_lists_only_scoped_unconfirmed_candidates(tmp_path):
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    store = RuntimeContinuityStore(tmp_path)
    matching = store.propose_candidate(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["mantengo", "le", "promesse"],
    )
    store.propose_candidate(
        runtime_scope_id="scope-2",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["resta", "privato"],
    )
    provider = RelicMemoryProvider(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        relic_home=str(tmp_path),
    )

    assert provider.pending_candidates() == [
        {
            "marker_id": matching["marker_id"],
            "wording": "mantengo le promesse",
        }
    ]
    assert provider.prefetch("") == ""


def test_memory_provider_confirmation_surfaces_only_its_scoped_candidate(tmp_path):
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    store = RuntimeContinuityStore(tmp_path)
    matching = store.propose_candidate(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["mantengo", "le", "promesse"],
    )
    other_scope = store.propose_candidate(
        runtime_scope_id="scope-2",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["resta", "privato"],
    )
    provider = RelicMemoryProvider(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        relic_home=str(tmp_path),
    )

    assert provider.confirm_candidate(other_scope["marker_id"]) is None
    assert provider.confirm_candidate(matching["marker_id"]) is not None
    assert provider.prefetch("") == "mantengo le promesse"


def test_candidate_rejection_removes_only_its_scoped_marker(tmp_path):
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    store = RuntimeContinuityStore(tmp_path)
    candidate = store.propose_candidate(
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        user_words=["non", "presumere"],
    )

    assert (
        store.reject_candidate(
            marker_id=candidate["marker_id"],
            runtime_scope_id="scope-2",
            hermes_profile_id=HERMES_USER_PROFILE_NAME,
        )
        is None
    )
    assert store.get_recent_candidates(
        runtime_scope_id="scope-1", hermes_profile_id=HERMES_USER_PROFILE_NAME
    ) == [candidate]
    assert store.reject_candidate(
        marker_id=candidate["marker_id"],
        runtime_scope_id="scope-1",
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
    ) == candidate
    assert store.get_recent_candidates(
        runtime_scope_id="scope-1", hermes_profile_id=HERMES_USER_PROFILE_NAME
    ) == []
    assert store.get_recent_markers(
        runtime_scope_id="scope-1", hermes_profile_id=HERMES_USER_PROFILE_NAME
    ) == []
