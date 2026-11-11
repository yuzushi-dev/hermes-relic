"""Bounded canonical actions for managed User cron scripts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relic.chronicle.context import set_runtime_scope_id
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME, validate_runtime_scope_id

_CHECKIN_DELIVERY_WINDOWS = ("09:30-12:30", "15:00-19:30", "21:00-22:30")
# Match relic-oss: one shared three-hour gap leaves room for the
# morning → diegetic → proactive → evening cadence.
_INITIATIVE_MIN_SPACING_SECONDS = 3 * 60 * 60
_DELIVERY_RESERVATION_TTL_SECONDS = 10 * 60
_CHECKIN_GUARD_STATE = Path("state/checkin_guard.json")
_DELIVERY_KIND_BY_ACTION = {
    "checkin-message": "checkin",
    "proactive-message": "proactivity",
    "diegetic-message": "diegetic",
}
_MESSAGE_JOB_NAME_BY_KIND = {
    "checkin": "gumi_checkin_message",
    "proactivity": "gumi_proactive_message",
    "diegetic": "gumi_diegetic_message",
}
_FORCE_ACTIONS = {"diegetic-gate", "diegetic-message", "checkin-gate", "checkin-message"}

# Canonical hour bands, mirroring relic.checkin.features.CHECKIN_SLOT_WINDOWS.
_SLOT_BANDS: dict[str, tuple[int, int]] = {
    "morning": (8, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
}

# Retained for compatibility helpers; delivery decisions use relic.checkin.policy.
_NON_RESPONSE_BACKOFF_THRESHOLD = 3

# Queue candidates may suggest any non-silent composer posture.
_PROACTIVE_POSTURES = frozenset(
    {
        "observe",
        "brief_share",
        "ask",
        "follow_up_warm",
        "follow_up_terse",
        "reflective_mirror",
        "small_share",
        "repair",
    }
)


CANONICAL_ACTIONS = (
    "continuity-sync",
    "checkin-gate",
    "checkin-message",
    "continuity-dispatch",
    "proactive-gate",
    "proactive-message",
    "diegetic-gate",
    "diegetic-message",
    "continuity-compaction",
    "continuity-review",
    "memory-exposure-rollup",
    "provider-eval-rollup",
)


class CanonicalActionError(RuntimeError):
    """A canonical cron action cannot proceed safely."""


@dataclass(frozen=True)
class _CanonicalRuntime:
    scope_id: str
    relic_home: Path
    hermes_home: Path
    artifacts: Mapping[str, Mapping[str, Any]]
    force: bool = False


def resolve_runtime_scope_id(*, relic_home: Path) -> str:
    """Load the validated runtime scope from the canonical User profile."""
    _, user = _load_profile(relic_home)
    return user.runtime_scope_id


def dispatch_action(
    action: str,
    *,
    relic_home: Path,
    runtime_scope_id: str,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run one bounded canonical action without dispatching delivery."""
    if action not in CANONICAL_ACTIONS:
        raise CanonicalActionError("unsupported canonical action")
    runtime = _load_runtime(relic_home, runtime_scope_id)
    if force:
        if action not in {"checkin-message", "proactive-message", "diegetic-message"}:
            raise CanonicalActionError("manual flags require a message action")
        runtime = replace(runtime, force=True)
    if action == "continuity-sync":
        return _sync_context(action, runtime)
    if action in {"checkin-gate", "proactive-gate", "diegetic-gate"}:
        return _gate_context(action, runtime, now)
    if action in {"checkin-message", "continuity-dispatch", "proactive-message", "diegetic-message"}:
        return _message_context(action, runtime, now)
    # Maintenance jobs receive a deterministic snapshot gate. Other rollups keep
    # their existing bounded status context.
    return _continuity_context(action, runtime, now)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relic.profile.user_runtime", allow_abbrev=False)
    parser.add_argument("--relic-home", type=Path, required=True)
    parser.add_argument("--runtime-scope-id", required=True)
    parser.add_argument("--action", choices=CANONICAL_ACTIONS, required=True)
    parser.add_argument("--force", action="store_true", help="Run the stored message job, bypassing cadence but preserving consent and safety.")
    parser.add_argument("--dryrun", action="store_true", help="Run in temporary state with simulated dispatch; never send Telegram or generate media.")
    args = parser.parse_args(argv)
    if (args.force or args.dryrun) and args.action not in _DELIVERY_KIND_BY_ACTION:
        parser.error("--force and --dryrun require a message action")
    try:
        if args.force or args.dryrun:
            from relic.profile.manual_cron import run_manual

            context = run_manual(
                action=args.action, relic_home=args.relic_home,
                runtime_scope_id=args.runtime_scope_id, force=args.force, dryrun=args.dryrun,
            )
        else:
            if os.environ.get("_RELIC_MANUAL_DRYRUN") == "1":
                from relic.profile.manual_cron import install_dryrun_guard

                install_dryrun_guard()
            context = dispatch_action(
                args.action,
                relic_home=args.relic_home,
                runtime_scope_id=args.runtime_scope_id,
                **({"force": True} if os.environ.get("_RELIC_MANUAL_FORCE") == "1" else {}),
            )
    except CanonicalActionError as exc:
        print(f"CANONICAL_ACTION_ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(context, sort_keys=True))
    return 1 if context.get("ok") is False else 0

def _hermes_profiles_home() -> Path | None:
    configured = os.environ.get("HERMES_PROFILES_HOME", "").strip()
    if configured:
        return Path(configured)
    raw_home = os.environ.get("HERMES_HOME", "").strip()
    if not raw_home:
        return None
    home = Path(raw_home)
    if home.name == HERMES_USER_PROFILE_NAME and home.parent.name == "profiles":
        return home.parent
    return home / "profiles"


def _load_profile(relic_home: Path) -> tuple[Any, Any]:
    from relic.profile.registry import ProfileRegistry

    profile_path = relic_home / "user" / "user_profile.json"
    try:
        profile_stat = profile_path.lstat()
    except OSError as exc:
        raise CanonicalActionError("canonical profile is unavailable") from exc
    if not stat.S_ISREG(profile_stat.st_mode) or stat.S_IMODE(profile_stat.st_mode) != 0o600:
        raise CanonicalActionError("canonical profile is unavailable")
    registry = ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=_hermes_profiles_home(),
    )
    try:
        user = registry.load_user()
    except (OSError, ValueError) as exc:
        raise CanonicalActionError("canonical profile is unavailable") from exc
    if user is None:
        raise CanonicalActionError("canonical profile is unavailable")
    return registry, user




def _load_runtime(relic_home: Path, runtime_scope_id: str) -> _CanonicalRuntime:
    registry, user = _load_profile(relic_home)
    try:
        supplied_scope_id = validate_runtime_scope_id(runtime_scope_id)
    except ValueError as exc:
        raise CanonicalActionError("runtime scope is invalid") from exc
    if supplied_scope_id != user.runtime_scope_id:
        raise CanonicalActionError("runtime scope does not match the canonical profile")
    set_runtime_scope_id(supplied_scope_id)
    try:
        artifacts = registry.load_user_artifacts()
    except (OSError, ValueError) as exc:
        raise CanonicalActionError("canonical runtime state is unavailable") from exc
    if artifacts is None:
        raise CanonicalActionError("canonical runtime state is unavailable")
    runtime = _CanonicalRuntime(
        scope_id=supplied_scope_id,
        relic_home=relic_home,
        hermes_home=registry.hermes_user_home,
        artifacts=artifacts,
    )
    _ensure_checkin_store(runtime)
    return runtime
def _write_private_sidecar(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CanonicalActionError("canonical check-in sidecar is invalid")
    if path.exists():
        path.chmod(0o600)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".checkin-sidecar.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _checkin_store_paths(runtime: _CanonicalRuntime) -> tuple[Path, Path, Path, Path]:
    """Return the private singleton check-in database, baseline, policy, queue."""
    checkin_dir = runtime.relic_home / "user" / "checkin"
    checkin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if checkin_dir.is_symlink() or not checkin_dir.is_dir():
        raise CanonicalActionError("canonical check-in store is invalid")
    checkin_dir.chmod(0o700)
    db_path = checkin_dir / "relic.db"
    if db_path.is_symlink() or (db_path.exists() and not db_path.is_file()):
        raise CanonicalActionError("canonical check-in database is invalid")
    from relic.checkin.db_init import init_db, seed_facets

    conn = init_db(db_path)
    try:
        seed_facets(conn)
    finally:
        conn.close()
    db_path.chmod(0o600)
    baseline_artifact = runtime.artifacts.get("baseline_response")
    baseline = (
        baseline_artifact.get("baseline")
        if isinstance(baseline_artifact, Mapping)
        and isinstance(baseline_artifact.get("baseline"), Mapping)
        else {}
    )
    baseline_path = checkin_dir / "baseline.json"
    _write_private_sidecar(baseline_path, baseline)
    policy_path = checkin_dir / "delivery_policy.json"
    _write_private_sidecar(
        policy_path,
        {
            "consent_for_active_elicitation": False,
            "consent_for_passive_extraction": False,
        },
    )
    queue_path = checkin_dir / "proactive_queue.jsonl"
    if queue_path.is_symlink() or (queue_path.exists() and not queue_path.is_file()):
        raise CanonicalActionError("canonical proactive queue is invalid")
    if queue_path.exists():
        queue_path.chmod(0o600)
    return db_path, baseline_path, policy_path, queue_path


def _ensure_checkin_store(runtime: _CanonicalRuntime) -> None:
    _checkin_store_paths(runtime)


def _sync_user_checkin(runtime: _CanonicalRuntime) -> None:
    db_path, baseline_path, policy_path, _queue_path = _checkin_store_paths(runtime)
    state_db_path = runtime.hermes_home / "state.db"
    if not state_db_path.exists():
        return
    import sqlite3

    from relic.checkin.facet_updater import process_pending_exchanges
    from relic.checkin.passive_extractor import run_passive_extraction
    from relic.shared_continuity.runtime_store import RuntimeContinuityStore

    conn = sqlite3.connect(str(db_path))
    try:
        facets = {
            row[0]: {
                "name": row[1],
                "description": row[2],
                "spectrum_low": row[3],
                "spectrum_high": row[4],
            }
            for row in conn.execute(
                "SELECT id, name, description, spectrum_low, spectrum_high FROM facets"
            )
        }
        run_passive_extraction(
            conn,
            runtime.scope_id,
            state_db_path,
            facets,
            relic_home=runtime.relic_home,
            delivery_policy_path=policy_path,
        )
        process_pending_exchanges(
            conn,
            baseline_path,
            runtime.scope_id,
            continuity_store=RuntimeContinuityStore(runtime.relic_home),
            hermes_profile_id=HERMES_USER_PROFILE_NAME,
        )
    finally:
        conn.close()


def _context(
    action: str,
    runtime: _CanonicalRuntime,
    status: str,
    context: str,
    *,
    ask_topic: str | None = None,
    checkin_context: str | None = None,
    gate: str | None = None,
    delivery_context: str | None = None,
    media_type: str | None = None,
    wake_agent: bool | None = None,
    proactive_signal_ref: str | None = None,
    posture: str | None = None,
    policy_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "context": context,
        "runtime_scope_id": runtime.scope_id,
        "status": status,
    }
    if ask_topic:
        payload["ask_topic"] = ask_topic
    if checkin_context:
        payload["checkin_context"] = checkin_context
    if gate:
        payload["gate"] = gate
    if delivery_context:
        payload["delivery_context"] = delivery_context
    if media_type:
        payload["media_type"] = media_type
    if proactive_signal_ref:
        payload["proactive_signal_ref"] = proactive_signal_ref
    if posture:
        payload["posture"] = posture
    if policy_reason:
        payload["policy_reason"] = policy_reason
    if wake_agent is not None:
        payload["wakeAgent"] = wake_agent
    return payload


def _sync_context(action: str, runtime: _CanonicalRuntime) -> dict[str, Any]:
    from relic.gumi_plugin.memory_sync import sync

    try:
        result = sync(runtime.hermes_home)
    except Exception as exc:
        raise CanonicalActionError("continuity sync failed") from exc
    if not isinstance(result, Mapping):
        raise CanonicalActionError("continuity sync returned invalid state")
    errors = result.get("errors", 0)
    appended = result.get("appended", 0)
    if type(errors) is not int or type(appended) is not int:
        raise CanonicalActionError("continuity sync returned invalid state")
    if errors:
        raise CanonicalActionError("continuity sync failed")
    legacy_subject = os.environ.get("RELIC_SUBJECT_ID", "").strip()
    try:
        if legacy_subject:
            from relic.checkin.facet_updater import process_pending_exchanges
            from relic.checkin.passive_extractor import run_for_hermes_home

            run_for_hermes_home(
                runtime.hermes_home,
                relic_home=runtime.relic_home,
                subject_id=legacy_subject,
            )
            subject_dir = runtime.relic_home / "subjects" / legacy_subject
            db_path = subject_dir / "relic.db"
            baseline_path = subject_dir / "subject_baseline.json"
            if db_path.exists() and baseline_path.exists():
                import sqlite3

                conn = sqlite3.connect(str(db_path))
                try:
                    process_pending_exchanges(conn, baseline_path, legacy_subject)
                finally:
                    conn.close()
        else:
            _sync_user_checkin(runtime)
    except Exception:
        # Check-in extraction is consent-gated and must never break continuity
        # synchronization or make the cron job fail closed.
        pass
    return _context(action, runtime, "actionable" if appended else "no-change", "ready")


def _current_slot(local_now: datetime) -> str | None:
    """Return the canonical slot name for an hour, or None outside all bands."""
    hour = local_now.hour
    for slot, (start, end) in _SLOT_BANDS.items():
        if start <= hour < end:
            return slot
    return None

def _enabled_checkin_slots(runtime: _CanonicalRuntime) -> set[str]:
    """Read the subject's reserved check-in slots from the canonical profile.

    Fails open to an empty set (no reservation enforced) when the profile
    does not configure ``preferences.safety.checkin_slots``.
    """
    safety = _safety_preferences(runtime)
    slots = safety.get("checkin_slots")
    if not isinstance(slots, list):
        return set()
    return {slot for slot in slots if slot in _SLOT_BANDS}


def _safety_preferences(runtime: _CanonicalRuntime) -> Mapping[str, Any]:
    gumi_profile = runtime.artifacts.get("gumi_profile")
    preferences = gumi_profile.get("preferences") if isinstance(gumi_profile, Mapping) else None
    safety = preferences.get("safety") if isinstance(preferences, Mapping) else None
    return safety if isinstance(safety, Mapping) else {}


def _checkin_slot_reserved(
    action: str, runtime: _CanonicalRuntime, now: datetime | None
) -> bool:
    """Enforce reserved check-in slots, mirroring relic.checkin.policy: when
    the subject's profile designates specific slots for check-ins, check-in
    delivery is confined to those slots, and proactive/diegetic delivery is
    confined to the residual (non check-in) slots. Fails open (no
    restriction) when no slots are configured or the current hour falls
    outside every band.
    """
    enabled = _enabled_checkin_slots(runtime)
    if not enabled:
        return True
    current = _current_slot(_local_now(runtime, now))
    if current is None:
        return True
    if action in {"checkin-gate", "checkin-message"}:
        return current in enabled
    if action in {"proactive-gate", "proactive-message", "diegetic-gate", "diegetic-message"}:
        return current not in enabled
    return True


def _gate_line(action: str, runtime: _CanonicalRuntime, now: datetime | None) -> str:
    local_now = _local_now(runtime, now)
    media_type = "text"
    if action in {"proactive-gate", "proactive-message", "diegetic-gate", "diegetic-message"}:
        media_type = _media_type(runtime, now)
    lines = [
        "DELIVER",
        f"tipo: {media_type}",
        f"ora: {local_now.strftime('%H:%M %Z')}",
    ]
    if action in {"proactive-gate", "proactive-message"}:
        lines.append("proactive: true")
    if action in {"diegetic-gate", "diegetic-message"}:
        hook_seed = f"{runtime.scope_id}|diegetic|{local_now.date()}".encode()
        lines.append(f"hook: {'true' if int(hashlib.sha256(hook_seed).hexdigest(), 16) % 100 < 35 else 'false'}")
    return "\n".join(lines)


def _canonical_media_allowed(runtime: _CanonicalRuntime, media_type: str) -> bool:
    policy_path = runtime.relic_home / "user" / "checkin" / "delivery_policy.json"
    key = {"image": "PRO_IMAGE", "voice": "PRO_AUDIO", "music": "PRO_LYRIA"}[media_type]
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        return int(policy.get(key, 2)) > 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return True


def _media_type(runtime: _CanonicalRuntime, now: datetime | None) -> str:
    if os.environ.get("RELIC_FORCE_CHECKIN", "").lower() in {"1", "true", "yes"}:
        forced_media = os.environ.get("RELIC_FORCE_MEDIA_TYPE", "").strip().lower()
        if forced_media in {"voice", "image", "music", "text"}:
            return forced_media
    from relic.gumi_plugin.cron_wiring import (
        MEDIA_COOLDOWN_DAYS,
        _MEDIA_PROB_THRESHOLDS,
    )
    from relic.gumi_plugin.media_state import is_media_eligible, last_media_ts

    profile = runtime.artifacts.get("gumi_profile")
    media = profile.get("media") if isinstance(profile, Mapping) else None
    if not isinstance(media, Mapping):
        return "text"
    media_keys = {"image": "images", "voice": "audio", "music": "music"}
    eligible: dict[str, bool] = {}
    current = _utc_now(now)
    for media_type, media_key in media_keys.items():
        item = media.get(media_key)
        if (
            isinstance(item, Mapping)
            and item.get("selected") is True
            and item.get("enabled") is True
            and _canonical_media_allowed(runtime, media_type)
        ):
            eligible[media_type] = is_media_eligible(
                runtime.hermes_home,
                media_type,
                current,
                MEDIA_COOLDOWN_DAYS,
            )
    if not any(eligible.values()):
        return "text"

    if eligible.get("music"):
        last_music = last_media_ts(runtime.hermes_home, "music")
        if last_music is None or (current - last_music).days >= 14:
            return "music"

    seed = f"{runtime.scope_id}|media|{_local_now(runtime, now).date()}"
    roll = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 100
    for media_type in ("music", "voice", "image"):
        if eligible.get(media_type) and roll < _MEDIA_PROB_THRESHOLDS[media_type]:
            return media_type
    return "text"


def _gate_context(
    action: str, runtime: _CanonicalRuntime, now: datetime | None
) -> dict[str, Any]:
    if action in {
        "checkin-gate",
        "checkin-message",
        "proactive-gate",
        "proactive-message",
        "diegetic-gate",
        "diegetic-message",
    }:
        allowed = _delivery_armed(runtime)
    else:
        allowed = _delivery_armed(runtime) and not _in_quiet_hours(runtime, now) and _cadence_due(
            runtime, now
        )
    posture = None
    policy_reason = None
    if not allowed:
        return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
    if (
        not runtime.force
        and action in _FORCE_ACTIONS
        and os.environ.get("RELIC_FORCE_CHECKIN", "").lower() in {"1", "true", "yes"}
    ):
        if _in_quiet_hours(runtime, now):
            return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
        return _context(
            action,
            runtime,
            "eligible",
            "ready",
            gate=_gate_line(action, runtime, now),
            wake_agent=True,
            posture="brief_share" if action in _FORCE_ACTIONS else None,
            policy_reason="forced",
        )
    if action in {
        "checkin-gate",
        "checkin-message",
        "proactive-gate",
        "proactive-message",
        "diegetic-gate",
        "diegetic-message",
    }:
        allowed, posture, policy_reason = _policy_outcome(action, runtime, now)
    if not allowed:
        return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
    gate = _gate_line(action, runtime, now)
    return _context(
        action,
        runtime,
        "eligible",
        "ready",
        gate=gate,
        wake_agent=True,
        posture=posture,
        policy_reason=policy_reason,
    )


def _policy_outcome(
    action: str, runtime: _CanonicalRuntime, now: datetime | None
) -> tuple[bool, str, str]:
    """Run the reference relic-oss feature/policy pipeline on singleton paths."""
    from relic.checkin.features import (
        build_checkin_features,
        compute_reach_score,
        persist_features,
    )
    from relic.checkin.policy import EventType, select_decision

    decision_type = {
        "checkin-gate": "checkin",
        "checkin-message": "checkin",
        "proactive-gate": "proactivity",
        "proactive-message": "proactivity",
        "diegetic-gate": "diegetic",
        "diegetic-message": "diegetic",
    }[action]
    db_path, baseline_path, policy_path, queue_path = _checkin_store_paths(runtime)
    from relic.checkin.outcome_reconciler import reconcile_due_outcomes

    reconcile_due_outcomes(
        runtime.scope_id,
        relic_home=runtime.relic_home,
        hermes_home=runtime.hermes_home,
        now=_utc_now(now),
        db_path=db_path,
    )
    features = build_checkin_features(
        subject_id=runtime.scope_id,
        decision_type=decision_type,
        relic_home=runtime.relic_home,
        hermes_home=runtime.hermes_home,
        now=_utc_now(now),
        subject_dir=db_path.parent,
        baseline_path=baseline_path,
    )

    deliveries, _pending, _kind = _load_delivery_state(
        _delivery_state_path(runtime.hermes_home)
    )
    current = _utc_now(now)
    if deliveries:
        last_delivery = max(deliveries)
        features.last_delivered_initiative_at = last_delivery
        features.time_since_last_initiative_sec = max(
            0, int((current - last_delivery).total_seconds())
        )
    local_day = _local_now(runtime, now).date()
    features.daily_initiatives_today = sum(
        _local_now(runtime, value).date() == local_day for value in deliveries
    )
    limit, span_days = _frequency_spec(runtime)
    features.frequency_cap_per_day = limit if span_days == 1 else None
    if span_days > 1:
        cutoff = current - timedelta(days=span_days)
        features.rolling_initiative_cap = limit
        features.rolling_initiatives_in_window = sum(
            value >= cutoff for value in deliveries
        )
    features.minimum_initiative_gap_sec = _INITIATIVE_MIN_SPACING_SECONDS
    features.delivery_window_open = _in_delivery_window(
        _local_now(runtime, now), _delivery_windows(runtime)
    )

    streak, _last_reply_at = _load_streak_state(
        _delivery_state_path(runtime.hermes_home)
    )
    features.non_response_streak = max(features.non_response_streak, streak)
    features.reach_score = compute_reach_score(
        features.non_response_streak,
        features.followup_non_response_streak,
    )
    features.enabled_checkin_slots = sorted(_enabled_checkin_slots(runtime))
    features.current_checkin_slot = _current_slot(_local_now(runtime, now))
    features.quiet_hours_active = _in_quiet_hours(runtime, now)

    safety = _safety_preferences(runtime)
    features.diegetic_enabled = bool(safety.get("diegetic_enabled", False))
    cap = safety.get("maximum_daily_initiatives")
    if isinstance(cap, int) and not isinstance(cap, bool):
        features.frequency_cap_per_day = cap

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        policy = {}
    features.consent_active = (
        isinstance(policy, Mapping)
        and policy.get("consent_for_active_elicitation") is True
    )

    if decision_type == "proactivity" and queue_path.exists():
        features.salience_top = max(features.salience_top, _queued_salience(queue_path))

    decision = select_decision(
        features,
        decision_type=decision_type,
        policy_enabled=True,
        reflection_enabled=False,
        **({"force": True} if runtime.force else {}),
    )
    expected = {
        "checkin": EventType.CHECKIN,
        "proactivity": EventType.PROACTIVE,
        "diegetic": EventType.DIEGETIC,
    }[decision_type]
    if decision.event_type is expected and action.endswith("-message"):
        import sqlite3

        with sqlite3.connect(db_path) as connection:
            persist_features(
                connection,
                runtime.scope_id,
                current.isoformat(),
                features,
                decision.posture.value,
            )
    return (
        decision.event_type is expected,
        decision.posture.value,
        decision.reason,
    )


def _queued_salience(path: Path) -> float:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    priorities: list[float] = []
    for line in rows:
        try:
            row = json.loads(line)
            if not isinstance(row, Mapping) or row.get("consumed_at"):
                continue
            priorities.append(float(row.get("priority", 0.0)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(priorities, default=0.0)


def _initiative_context(
    action: str,
    runtime: _CanonicalRuntime,
    now: datetime | None,
    *,
    posture: str = "brief_share",
) -> tuple[str, str]:
    from relic.checkin.context_builder import build_deliver_context

    db_path, baseline_path, policy_path, _queue_path = _checkin_store_paths(runtime)
    media_type = _media_type(runtime, now)
    posture = posture if posture in _PROACTIVE_POSTURES else "brief_share"
    event_type = "proactive" if action == "proactive-message" else "diegetic"
    context = build_deliver_context(
        runtime.scope_id,
        runtime.hermes_home,
        runtime.relic_home,
        event_type=event_type,
        posture=posture,
        persist_topic_hint=False,
        media_type=media_type,
        db_path=db_path,
        baseline_path=baseline_path,
        policy_path=policy_path,
        now=_utc_now(now),
    )
    return context, media_type


def _message_context(
    action: str, runtime: _CanonicalRuntime, now: datetime | None
) -> dict[str, Any]:
    _validate_media(runtime)
    gate = _gate_context(action, runtime, now)
    if gate["status"] == "silent":
        return gate
    if action in {"checkin-message", "diegetic-message"}:
        if not _reserve_delivery(runtime, now, action):
            return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
    selected_ask_topic = (
        _checkin_ask_topic(runtime, now) if action == "checkin-message" else None
    )
    checkin_context = (
        _build_checkin_context(runtime, now, selected_ask_topic)
        if action == "checkin-message"
        else None
    )
    delivery_context = None
    media_type = None
    proactive_signal_ref = None
    initiative_posture = str(gate.get("posture") or "brief_share")
    if action in {"proactive-message", "diegetic-message"}:
        if action == "proactive-message" and runtime.force:
            if not _reserve_delivery(runtime, now, action):
                return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
        elif action == "proactive-message":
            from relic.hermes_runtime import RuntimeDecision
            from relic.gumi_plugin.proactive_consumer import consume_one

            _db_path, _baseline_path, _policy_path, queue_path = _checkin_store_paths(runtime)
            if queue_path.exists():
                if not _reserve_delivery(runtime, now, action):
                    return _context(
                        action, runtime, "silent", "[SILENT]", wake_agent=False
                    )
                try:
                    decision, _reasons, candidate = consume_one(
                        runtime.scope_id,
                        runtime.hermes_home,
                        runtime.relic_home,
                        now=_utc_now(now),
                        queue_path=queue_path,
                    )
                except BaseException:
                    _cancel_delivery_reservation(runtime, action)
                    raise
                if decision is not RuntimeDecision.DELIVER:
                    _cancel_delivery_reservation(runtime, action)
                    return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
                if isinstance(candidate, Mapping):
                    candidate_posture = candidate.get("suggested_posture")
                    if (
                        isinstance(candidate_posture, str)
                        and candidate_posture in _PROACTIVE_POSTURES
                    ):
                        initiative_posture = candidate_posture
                    proactive_signal_ref = (
                        str(candidate.get("proactive_signal_ref"))
                        if candidate.get("proactive_signal_ref")
                        else None
                    )
            else:
                if not _uses_legacy_proactive_lane(runtime):
                    return _context(action, runtime, "silent", "[SILENT]", wake_agent=False)
                # Legacy profile artifacts keep the relic-oss generic lane.
                if not _reserve_delivery(runtime, now, action):
                    return _context(
                        action, runtime, "silent", "[SILENT]", wake_agent=False
                    )
        delivery_context, media_type = _initiative_context(
            action, runtime, now, posture=initiative_posture
        )
    # The context bundle is the sanitized source of truth. Never expose the
    # raw selector hint: it can bypass anti-repeat and clinical-term filters.
    return _context(
        action,
        runtime,
        "ready",
        "ready",
        checkin_context=checkin_context,
        gate=gate.get("gate"),
        delivery_context=delivery_context,
        media_type=media_type,
        proactive_signal_ref=proactive_signal_ref,
        posture=initiative_posture if action in {"proactive-message", "diegetic-message"} else None,
        policy_reason=gate.get("policy_reason"),
        wake_agent=True,
    )




def _checkin_ask_topic(runtime: _CanonicalRuntime, now: datetime | None) -> str | None:
    """Resolve a facet question from the private singleton User store."""
    try:
        db_path, baseline_path, _policy_path, _queue_path = _checkin_store_paths(runtime)
        from relic.gumi_plugin.cron_wiring import _select_ask_decision

        ask, topic = _select_ask_decision(
            runtime.scope_id,
            _utc_now(now),
            runtime.relic_home,
            db_path=db_path,
            baseline_path=baseline_path,
        )
    except Exception:
        return None
    return topic if ask and isinstance(topic, str) and topic else None


def _build_checkin_context(
    runtime: _CanonicalRuntime, now: datetime | None, ask_topic: str | None
) -> str | None:
    """Build the sanitized check-in context from the singleton store."""
    if not ask_topic:
        return None
    try:
        db_path, baseline_path, policy_path, _queue_path = _checkin_store_paths(runtime)
        if runtime.force:
            from relic.checkin.context_builder import select_topic_hint

            selected = select_topic_hint(runtime.scope_id, db_path, baseline_path, now=_utc_now(now))
            return selected["hint"] if selected else None
        from relic.checkin.context_builder import build_deliver_context
        from relic.checkin.topic_hint import HEADER

        bundle = build_deliver_context(
            runtime.scope_id,
            runtime.hermes_home,
            runtime.relic_home,
            event_type="checkin",
            posture="ask",
            persist_topic_hint=True,
            db_path=db_path,
            baseline_path=baseline_path,
            policy_path=policy_path,
            now=_utc_now(now),
            **({"force": True} if runtime.force else {}),
        )
        return bundle if bundle and HEADER in bundle else None
    except Exception:
        return None


def _continuity_context(
    action: str, runtime: _CanonicalRuntime, now: datetime | None
) -> dict[str, Any]:
    if action in {"continuity-compaction", "continuity-review"}:
        from relic.profile.maintenance import prepare_run

        kind = {
            "continuity-compaction": "state_compaction",
            "continuity-review": "candidate_review",
        }[action]
        return prepare_run(runtime.hermes_home / "workspace" / "gumi", kind)
    return _context(
        action,
        runtime,
        "actionable" if _continuity_markers(runtime, now) else "no-change",
        "ready",
    )


def _delivery_armed(runtime: _CanonicalRuntime) -> bool:
    policy = runtime.artifacts.get("runtime_policy")
    checkin_state = runtime.artifacts.get("checkin_state")
    if not isinstance(policy, Mapping) or not isinstance(checkin_state, Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    capability = policy.get("delivery_capability_enabled")
    armed = policy.get("delivery_execution_armed")
    recurring = checkin_state.get("recurring")
    if type(capability) is not bool or type(armed) is not bool or not isinstance(recurring, Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    status = recurring.get("status")
    if not isinstance(status, str):
        raise CanonicalActionError("canonical runtime state is invalid")
    return capability and armed and status == "armed"


def _validate_media(runtime: _CanonicalRuntime) -> None:
    profile = runtime.artifacts.get("gumi_profile")
    if not isinstance(profile, Mapping) or not isinstance(profile.get("media"), Mapping):
        raise CanonicalActionError("canonical media state is invalid")
    for name in ("images", "audio", "music"):
        item = profile["media"].get(name)
        if (
            not isinstance(item, Mapping)
            or type(item.get("selected")) is not bool
            or type(item.get("enabled")) is not bool
        ):
            raise CanonicalActionError("canonical media state is invalid")


def _delivery_windows(runtime: _CanonicalRuntime) -> list[str]:
    raw = _proactivity_preferences(runtime).get("delivery_windows")
    if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        return list(raw)
    return list(_CHECKIN_DELIVERY_WINDOWS)


def _in_delivery_window(current: datetime, windows: list[str]) -> bool:
    minute = current.hour * 60 + current.minute
    for value in windows:
        try:
            start_text, end_text = value.split("-", 1)
            start = _clock_minutes(start_text)
            end = _clock_minutes(end_text)
        except (AttributeError, ValueError):
            continue
        if start <= end:
            if start <= minute <= end:
                return True
        elif minute >= start or minute <= end:
            return True
    return False


def _frequency_spec(runtime: _CanonicalRuntime) -> tuple[int, int]:
    maximum_frequency = _proactivity_preferences(runtime).get("maximum_frequency")
    if not isinstance(maximum_frequency, str):
        raise CanonicalActionError("canonical runtime state is invalid")
    try:
        limit_text, window = maximum_frequency.split("/", 1)
        limit = int(limit_text)
    except ValueError as exc:
        raise CanonicalActionError("canonical runtime state is invalid") from exc
    if limit <= 0 or window not in {"day", "week"}:
        raise CanonicalActionError("canonical runtime state is invalid")
    return limit, (1 if window == "day" else 7)


def _delivery_state_path(hermes_home: Path) -> Path:
    return hermes_home / _CHECKIN_GUARD_STATE


def _parse_delivery_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_delivery_state(
    path: Path,
) -> tuple[list[datetime], datetime | None, str | None]:
    if not path.exists():
        return [], None, None
    if path.is_symlink() or not path.is_file():
        raise CanonicalActionError("canonical delivery state is invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = data.get("deliveries") if isinstance(data, Mapping) else None
        pending = data.get("pending_at") if isinstance(data, Mapping) else None
        kind = data.get("kind") if isinstance(data, Mapping) else None
        if not isinstance(values, list):
            raise ValueError
        deliveries = [_parse_delivery_timestamp(value).astimezone(timezone.utc) for value in values]
        pending_at = None if pending is None else _parse_delivery_timestamp(pending)
        kind = kind if isinstance(kind, str) else None
        return deliveries, pending_at, kind
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CanonicalActionError("canonical delivery state is invalid") from exc


def _read_raw_guard_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


@contextmanager
def _guard_lock(path: Path):
    lock_path = path.with_name("checkin_guard.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise CanonicalActionError("canonical delivery lock is invalid")
    with lock_path.open("a", encoding="utf-8") as stream:
        os.chmod(lock_path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_delivery_state_unlocked(
    path: Path,
    deliveries: list[datetime],
    pending_at: datetime | None,
    kind: str | None = None,
    pending_job_id: str | None = None,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CanonicalActionError("canonical delivery state is invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".checkin-guard.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        payload = _read_raw_guard_state(path)
        payload["deliveries"] = [value.astimezone(timezone.utc).isoformat() for value in deliveries[-32:]]
        payload["pending_at"] = pending_at.astimezone(timezone.utc).isoformat() if pending_at else None
        payload["kind"] = kind
        payload["pending_job_id"] = pending_job_id
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _managed_message_job_id(relic_home: Path, kind: str) -> str | None:
    name = _MESSAGE_JOB_NAME_BY_KIND.get(kind)
    if name is None:
        return None
    path = Path(relic_home) / "user" / "managed_cron.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            return None
        jobs = payload.get("jobs")
        record = jobs.get(name) if isinstance(jobs, Mapping) else None
        job_id = record.get("id") if isinstance(record, Mapping) else None
        return job_id if isinstance(job_id, str) and job_id else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _reserve_delivery(
    runtime: _CanonicalRuntime, now: datetime | None, action: str
) -> bool:
    """Set the short-lived anti-race lock (`pending_at`/`kind`) so a
    concurrent gate run cannot double-claim the same slot while a message
    job is composing its response. This does NOT count against the daily
    cap/spacing — the LLM may still abstain with [SILENT]. The cap/spacing
    entry in `deliveries` is only appended once delivery is confirmed, via
    `record_delivery_outcome`."""
    kind = _DELIVERY_KIND_BY_ACTION.get(action, action)
    job_id = _managed_message_job_id(runtime.relic_home, kind)
    if job_id is None:
        return False
    path = _delivery_state_path(runtime.hermes_home)
    current = datetime.now(timezone.utc)
    with _guard_lock(path):
        deliveries, pending_at, _kind = _load_delivery_state(path)
        if pending_at is not None:
            age = (current - pending_at.astimezone(timezone.utc)).total_seconds()
            if -60 <= age <= _DELIVERY_RESERVATION_TTL_SECONDS:
                return False
        _write_delivery_state_unlocked(path, deliveries, current, kind, job_id)
    return True


def _cancel_delivery_reservation(runtime: _CanonicalRuntime, action: str) -> None:
    """Release only the reservation claimed by this managed message lane."""
    kind = _DELIVERY_KIND_BY_ACTION.get(action, action)
    job_id = _managed_message_job_id(runtime.relic_home, kind)
    if job_id is None:
        return
    path = _delivery_state_path(runtime.hermes_home)
    with _guard_lock(path):
        deliveries, pending_at, stored_kind = _load_delivery_state(path)
        pending_job_id = _read_raw_guard_state(path).get("pending_job_id")
        if (
            pending_at is not None
            and stored_kind == kind
            and pending_job_id == job_id
        ):
            _write_delivery_state_unlocked(path, deliveries, None, None, None)


def _load_streak_state(path: Path) -> tuple[int, datetime | None]:
    """Read the non-response backoff streak and last subject-reply timestamp.

    Fails open to ``(0, None)`` on any malformed or missing state so a
    corrupt guard file never wrongly triggers or lifts a backoff.
    """
    data = _read_raw_guard_state(path)
    streak_raw = data.get("non_response_streak")
    streak = streak_raw if isinstance(streak_raw, int) and streak_raw >= 0 else 0
    reply_raw = data.get("last_reply_at")
    try:
        last_reply_at = _parse_delivery_timestamp(reply_raw) if reply_raw else None
    except ValueError:
        last_reply_at = None
    return streak, last_reply_at


def _write_streak_state_unlocked(
    path: Path, streak: int, last_reply_at: datetime | None
) -> None:
    """Merge the non-response streak fields into the shared guard state file
    without disturbing the delivery/pending/kind fields written elsewhere."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CanonicalActionError("canonical delivery state is invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".checkin-guard.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        payload = _read_raw_guard_state(path)
        payload.setdefault("deliveries", [])
        payload.setdefault("pending_at", None)
        payload.setdefault("kind", None)
        payload["non_response_streak"] = max(0, int(streak))
        payload["last_reply_at"] = (
            last_reply_at.astimezone(timezone.utc).isoformat() if last_reply_at else None
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_streak_state(path: Path, streak: int, last_reply_at: datetime | None) -> None:
    with _guard_lock(path):
        _write_streak_state_unlocked(path, streak, last_reply_at)


def record_subject_reply(
    hermes_home: Path,
    now: datetime | None = None,
    *,
    relic_home: Path | None = None,
    runtime_scope_id: str | None = None,
) -> None:
    """Record that the subject engaged (sent any live message), immediately
    resetting the non-response backoff streak. Best-effort: never raises,
    since this runs from a live conversation hook."""
    try:
        path = _delivery_state_path(Path(hermes_home))
        current = _utc_now(now)
        _write_streak_state(path, 0, current)
        _reconcile_canonical_cadence(
            relic_home,
            runtime_scope_id,
            outcome_status="answered",
            decision_type=None,
            now=current,
        )
    except Exception:
        pass


def _non_response_backoff_blocks(action: str, runtime: _CanonicalRuntime) -> bool:
    """Return True when consecutive un-replied checkin/proactive deliveries
    have reached the backoff threshold. Diegetic messages are unaffected
    (they use a separate, unrelated cadence in the original design)."""
    if action not in {"checkin-gate", "checkin-message", "proactive-gate", "proactive-message"}:
        return False
    streak, _last_reply_at = _load_streak_state(_delivery_state_path(runtime.hermes_home))
    return streak >= _NON_RESPONSE_BACKOFF_THRESHOLD


def consume_delivery_reservation(
    hermes_home: Path,
    session_id: str,
    *,
    relic_home: Path,
) -> tuple[bool, str | None]:
    """Consume the short-lived reservation before Hermes delivers cron output.

    Note: this only clears the reservation slot; it does NOT touch the
    non-response backoff streak. A consumed reservation means a message
    job was allowed to *attempt* delivery, not that content actually
    reached the subject (the LLM may still abstain with [SILENT], or the
    dispatcher may fail/skip). Call `record_delivery_outcome` once the
    real delivery outcome (sent vs. abstained) is known."""
    path = _delivery_state_path(Path(hermes_home))
    try:
        with _guard_lock(path):
            deliveries, pending_at, kind = _load_delivery_state(path)
            if pending_at is None or kind is None:
                return False, None
            age = (
                datetime.now(timezone.utc) - pending_at.astimezone(timezone.utc)
            ).total_seconds()
            if age > _DELIVERY_RESERVATION_TTL_SECONDS or age < -60:
                _write_delivery_state_unlocked(path, deliveries, None, None, None)
                return False, None
            raw = _read_raw_guard_state(path)
            pending_job_id = raw.get("pending_job_id")
            expected_job_id = _managed_message_job_id(Path(relic_home), kind)
            if (
                not isinstance(pending_job_id, str)
                or pending_job_id != expected_job_id
                or re.fullmatch(
                    rf"cron_{re.escape(pending_job_id)}_\d{{8}}_\d{{6}}",
                    session_id,
                )
                is None
            ):
                return False, None
            _write_delivery_state_unlocked(path, deliveries, None, None, None)
            return True, kind
    except (CanonicalActionError, OSError, ValueError, TypeError):
        return False, None


def record_delivery_outcome(
    hermes_home: Path,
    kind: str | None,
    now: datetime | None = None,
    *,
    relic_home: Path | None = None,
    runtime_scope_id: str | None = None,
) -> None:
    """Record a CONFIRMED delivery (real content actually reached the
    subject via Telegram) — never for a reservation that ended in LLM
    abstention ([SILENT]) or a failed dispatch.

    - checkin / proactivity / diegetic: append to the shared `deliveries`
      history so the daily cap and minimum spacing (`_checkin_delivery_due`)
      reflect only real contact, not mere attempts.
    Non-response is reconciled after the 24-hour response window, never at
    delivery time.

    Best-effort: never raises, since this runs from a live delivery hook.
    """
    if kind not in ("checkin", "proactivity", "diegetic"):
        return
    try:
        path = _delivery_state_path(Path(hermes_home))
        current = _utc_now(now)
        with _guard_lock(path):
            deliveries, pending_at, stored_kind = _load_delivery_state(path)
            pending_job_id = _read_raw_guard_state(path).get("pending_job_id")
            deliveries.append(current)
            _write_delivery_state_unlocked(
                path,
                deliveries,
                pending_at,
                stored_kind,
                pending_job_id if isinstance(pending_job_id, str) else None,
            )
        decision_type = {
            "checkin": "checkin",
            "proactivity": "proactivity",
            "diegetic": "diegetic",
        }[kind]
        if relic_home is not None and runtime_scope_id:
            _append_delivery_event(
                Path(relic_home), runtime_scope_id, decision_type, current
            )
            _reconcile_canonical_cadence(
                relic_home,
                runtime_scope_id,
                outcome_status="delivered",
                decision_type=decision_type,
                now=current,
            )
    except Exception:
        pass


def _append_delivery_event(
    relic_home: Path, runtime_scope_id: str, decision_type: str, now: datetime
) -> None:
    path = Path(relic_home) / "decision_events.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    event = {
        "created_at": now.isoformat(),
        "decision": "DELIVER",
        "decision_type": decision_type,
        "delivered_at": now.isoformat(),
        "gumi_instance_id": runtime_scope_id,
        "hermes_profile_id": HERMES_USER_PROFILE_NAME,
        "outcome_status": "delivered",
        "response_deadline_at": (now + timedelta(hours=24)).isoformat(),
        "subject_id": runtime_scope_id,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _reconcile_canonical_cadence(
    relic_home: Path | None,
    runtime_scope_id: str | None,
    *,
    outcome_status: str,
    decision_type: str | None,
    now: datetime,
) -> None:
    if relic_home is None or not runtime_scope_id:
        return
    import sqlite3

    from relic.checkin.features import (
        load_cadence_state,
        reconcile_cadence_outcome,
        save_cadence_state,
    )

    db_path = Path(relic_home) / "user" / "checkin" / "relic.db"
    if not db_path.is_file() or db_path.is_symlink():
        return
    with sqlite3.connect(db_path) as connection:
        state = load_cadence_state(connection, runtime_scope_id)
        state = reconcile_cadence_outcome(
            state,
            {
                "outcome_status": outcome_status,
                "decision_type": decision_type,
                "now": now,
            },
        )
        save_cadence_state(connection, state)
        connection.commit()

def _cadence_due(runtime: _CanonicalRuntime, now: datetime | None) -> bool:
    limit, span_days = _frequency_spec(runtime)
    current = _utc_now(now)
    cutoff = current.timestamp() - span_days * 24 * 60 * 60
    return (
        sum(
            marker.timestamp() >= cutoff
            for marker in _continuity_markers(runtime, current)
        )
        < limit
    )
def _in_quiet_hours(runtime: _CanonicalRuntime, now: datetime | None) -> bool:
    preferences = _proactivity_preferences(runtime)
    quiet_hours = preferences.get("quiet_hours")
    if not isinstance(quiet_hours, str):
        raise CanonicalActionError("canonical runtime state is invalid")
    try:
        start_text, end_text = quiet_hours.split("-", 1)
        start = _clock_minutes(start_text)
        end = _clock_minutes(end_text)
    except ValueError as exc:
        raise CanonicalActionError("canonical runtime state is invalid") from exc
    current = _local_now(runtime, now)
    minute = current.hour * 60 + current.minute
    return start <= minute < end if start < end else minute >= start or minute < end




def _proactivity_preferences(runtime: _CanonicalRuntime) -> Mapping[str, Any]:
    profile = runtime.artifacts.get("gumi_profile")
    if not isinstance(profile, Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    preferences = profile.get("preferences")
    if not isinstance(preferences, Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    proactivity = preferences.get("proactivity")
    if not isinstance(proactivity, Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    return proactivity
def _uses_legacy_proactive_lane(runtime: _CanonicalRuntime) -> bool:
    """Keep the pre-queue proactive lane for imported legacy artifacts."""
    profile = runtime.artifacts.get("gumi_profile")
    return isinstance(profile, Mapping) and profile.get("setup_mode") == "legacy_import"


def _continuity_markers(
    runtime: _CanonicalRuntime, now: datetime | None
) -> tuple[datetime, ...]:
    path = runtime.relic_home / "user" / "continuity.json"
    if not path.exists():
        return ()
    if path.is_symlink():
        raise CanonicalActionError("canonical continuity state is invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanonicalActionError("canonical continuity state is invalid") from exc
    markers = data.get("markers") if isinstance(data, Mapping) else None
    if not isinstance(markers, list):
        raise CanonicalActionError("canonical continuity state is invalid")
    values: list[datetime] = []
    for marker in markers:
        if not isinstance(marker, Mapping):
            raise CanonicalActionError("canonical continuity state is invalid")
        if (
            marker.get("runtime_scope_id") != runtime.scope_id
            or marker.get("hermes_profile_id") != HERMES_USER_PROFILE_NAME
            or marker.get("user_confirmation") is not True
        ):
            continue
        created_at = marker.get("created_at")
        if not isinstance(created_at, str):
            raise CanonicalActionError("canonical continuity state is invalid")
        try:
            value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CanonicalActionError("canonical continuity state is invalid") from exc
        values.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    return tuple(values)


def _local_now(runtime: _CanonicalRuntime, now: datetime | None) -> datetime:
    baseline = runtime.artifacts.get("baseline_response")
    if not isinstance(baseline, Mapping) or not isinstance(baseline.get("baseline"), Mapping):
        raise CanonicalActionError("canonical runtime state is invalid")
    zone_name = baseline["baseline"].get("timezone")
    current = _utc_now(now)
    if zone_name is None:
        return current
    if not isinstance(zone_name, str):
        raise CanonicalActionError("canonical runtime state is invalid")
    try:
        return current.astimezone(ZoneInfo(zone_name))
    except ZoneInfoNotFoundError as exc:
        raise CanonicalActionError("canonical runtime state is invalid") from exc


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    if len(hour) != 2 or len(minute) != 2:
        raise ValueError
    result = int(hour) * 60 + int(minute)
    if not 0 <= result < 24 * 60:
        raise ValueError
    return result


if __name__ == "__main__":
    raise SystemExit(main())
