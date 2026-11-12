"""One-way migration from a legacy subject profile to canonical User state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from relic.hermes_runtime import HERMES_USER_PROFILE_NAME, generate_runtime_scope_id
from relic.profile.registry import ProfileRegistry, UserProfile


_ALLOWED_PRIVATE_ENV = frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "GEMINI_API_KEY"})
_FORBIDDEN_KEY_TERMS = frozenset(
    {"subject", "experiment", "researcher", "consent", "emergency", "risk"}
)
_INTERNAL_KEY_TERMS = frozenset(
    {
        "bootstrap_session",
        "created_at",
        "creation_date",
        "updated_at",
        "provenance",
        "random_seed",
        "input_profile_hash",
        "gumi_instance_id",
        "researcher_review",
        "anti_clone",
        "arbitrary_opposite",
        "generation_mode",
        "profile_version",
        "schema_version",
        "version_history",
    }
)
_TELEGRAM_TOKEN = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_TARGET = re.compile(r"^-?\d+$")


class MigrationError(RuntimeError):
    """A legacy profile cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationResult:
    subject_id: str
    target_relic_home: Path
    target_hermes_home: Path
    dry_run: bool
    continuity_markers: int
    delivery_armed: bool


@dataclass(frozen=True)
class CheckinMigrationResult:
    subject_id: str
    runtime_scope_id: str
    target_db_path: Path
    dry_run: bool
    imported: bool


_CHECKIN_MIGRATION_TABLE = "_relic_checkin_migration"
_CHECKIN_MIGRATION_ID = "legacy-subject-v1"
_CHECKIN_DYNAMIC_TABLES = (
    "observations",
    "checkin_exchanges",
    "checkin_features",
    "checkin_cadence_state",
    "hypotheses",
    "model_snapshots",
    "passive_extraction_state",
)
_CHECKIN_SUBJECT_TABLES = (
    "checkin_features",
    "checkin_cadence_state",
    "passive_extraction_state",
)
_CHECKIN_CONSENT_DEFAULT = {
    "consent_for_active_elicitation": False,
    "consent_for_passive_extraction": False,
}


def migrate_legacy_checkin_store(
    source_subject: Path,
    *,
    target_relic_home: Path,
    expected_subject_id: str = "testsubject",
    dry_run: bool = False,
) -> CheckinMigrationResult:
    """Import legacy check-in state into a pristine canonical singleton.

    The gateway must be stopped while this migration runs. A completed import
    is a no-op; an import interrupted after database replacement resumes only
    the consent sidecar write.
    """
    source_subject = Path(source_subject).expanduser().resolve()
    target_relic_home = Path(target_relic_home).expanduser().resolve()
    subject_id = _subject_id(source_subject)
    if subject_id != expected_subject_id:
        raise MigrationError(f"migration is subject-scoped and expected {expected_subject_id!r}")
    runtime_scope_id = _canonical_runtime_scope(target_relic_home)
    checkin_dir = target_relic_home / "user" / "checkin"
    target_db = checkin_dir / "relic.db"
    policy_path = checkin_dir / "delivery_policy.json"
    _assert_checkin_paths_safe(checkin_dir, target_db, policy_path, dry_run=dry_run)

    marker = _target_marker_state(target_db, subject_id, runtime_scope_id)
    if marker is not None and marker[0] == "complete":
        return CheckinMigrationResult(
            subject_id, runtime_scope_id, target_db, dry_run, imported=False
        )

    consents = _read_checkin_consents(source_subject / "delivery_policy.json")
    if marker is not None and marker[0] == "db_imported":
        if not dry_run:
            if _checkin_policy_signature(policy_path) == marker[1]:
                _write_checkin_policy(policy_path, consents)
            _complete_checkin_migration(target_db, subject_id, runtime_scope_id)
        return CheckinMigrationResult(
            subject_id, runtime_scope_id, target_db, dry_run, imported=False
        )

    _assert_pristine_checkin_target(target_db, policy_path)
    source_db = source_subject / "relic.db"
    _validate_source_checkin_db(source_db)
    if not dry_run:
        checkin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        checkin_dir.chmod(0o700)
        _import_checkin_db(
            source_db,
            target_db,
            subject_id,
            runtime_scope_id,
            _checkin_policy_signature(policy_path),
        )
        _write_checkin_policy(policy_path, consents)
        _complete_checkin_migration(target_db, subject_id, runtime_scope_id)
    return CheckinMigrationResult(
        subject_id, runtime_scope_id, target_db, dry_run, imported=True
    )


def _canonical_runtime_scope(target_relic_home: Path) -> str:
    user_home = target_relic_home / "user"
    path = user_home / "user_profile.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise MigrationError("canonical singleton profile is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        return UserProfile.from_artifact(value, user_home=user_home).runtime_scope_id
    except MigrationError:
        raise
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MigrationError("canonical singleton profile is invalid") from exc


def _assert_checkin_paths_safe(
    checkin_dir: Path, db_path: Path, policy_path: Path, *, dry_run: bool
) -> None:
    if checkin_dir.is_symlink() or (checkin_dir.exists() and not checkin_dir.is_dir()):
        raise MigrationError("canonical check-in directory is invalid")
    for path in (db_path, policy_path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise MigrationError("canonical check-in target is invalid")
    if not dry_run and checkin_dir.exists():
        checkin_dir.chmod(0o700)


def _connect_readonly(path: Path, label: str) -> sqlite3.Connection:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise MigrationError(f"{label} is invalid")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except MigrationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError(f"{label} is invalid") from exc


def _expected_checkin_schema() -> dict[str, set[str]]:
    from relic.checkin.db_init import SCHEMA_SQL

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_SQL)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        return {
            table: {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for table in tables
        }
    finally:
        connection.close()


def _validate_checkin_connection(connection: sqlite3.Connection, label: str) -> None:
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise MigrationError(f"{label} failed integrity validation")
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for table, expected_columns in _expected_checkin_schema().items():
            if table not in actual_tables:
                raise MigrationError(f"{label} has an unsupported schema")
            actual_columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            if not expected_columns.issubset(actual_columns):
                raise MigrationError(f"{label} has an unsupported schema")
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationError(f"{label} is invalid") from exc


def _validate_source_checkin_db(path: Path) -> None:
    connection = _connect_readonly(path, "source check-in database")
    try:
        _validate_checkin_connection(connection, "source check-in database")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_CHECKIN_MIGRATION_TABLE,),
        ).fetchone():
            raise MigrationError("source check-in database has an unsupported schema")
    finally:
        connection.close()


def _target_marker_state(
    target_db: Path, subject_id: str, runtime_scope_id: str
) -> tuple[str, str] | None:
    if not target_db.exists():
        return None
    connection = _connect_readonly(target_db, "canonical check-in database")
    try:
        _validate_checkin_connection(connection, "canonical check-in database")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_CHECKIN_MIGRATION_TABLE,),
        ).fetchone():
            return None
        try:
            rows = connection.execute(
                f"SELECT migration_id, source_subject_id, runtime_scope_id, state, "
                f"original_policy_signature "
                f"FROM {_CHECKIN_MIGRATION_TABLE}"
            ).fetchall()
        except sqlite3.Error as exc:
            raise MigrationError("canonical check-in migration marker is invalid") from exc
        expected_prefix = (_CHECKIN_MIGRATION_ID, subject_id, runtime_scope_id)
        if len(rows) != 1 or rows[0][:3] != expected_prefix or rows[0][3] not in {
            "db_imported",
            "complete",
        }:
            raise MigrationError("canonical check-in migration marker is invalid")
        if not isinstance(rows[0][4], str) or not rows[0][4]:
            raise MigrationError("canonical check-in migration marker is invalid")
        return str(rows[0][3]), rows[0][4]
    finally:
        connection.close()


def _read_checkin_consents(path: Path) -> dict[str, bool]:
    if not path.is_file() or path.is_symlink():
        return dict(_CHECKIN_CONSENT_DEFAULT)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("legacy delivery policy is unreadable") from exc
    if not isinstance(value, dict):
        raise MigrationError("legacy delivery policy has an invalid schema")
    result: dict[str, bool] = {}
    for key in _CHECKIN_CONSENT_DEFAULT:
        consent = value.get(key, False)
        if not isinstance(consent, bool):
            raise MigrationError("legacy delivery policy has an invalid schema")
        result[key] = consent
    return result


def _checkin_policy_signature(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise MigrationError("canonical check-in policy is invalid") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise MigrationError("canonical check-in policy is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        return f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}:{digest}"
    finally:
        os.close(descriptor)


def _assert_pristine_checkin_target(target_db: Path, policy_path: Path) -> None:
    if policy_path.exists():
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError("canonical check-in policy is invalid") from exc
        if policy != _CHECKIN_CONSENT_DEFAULT:
            raise MigrationError("canonical check-in store is active unmarked")
    if not target_db.exists():
        return
    connection = _connect_readonly(target_db, "canonical check-in database")
    try:
        _validate_checkin_connection(connection, "canonical check-in database")
        if any(
            connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
            for table in _CHECKIN_DYNAMIC_TABLES
        ):
            raise MigrationError("canonical check-in store is active unmarked")
        sequence = connection.execute(
            "SELECT 1 FROM sqlite_sequence LIMIT 1"
        ).fetchone()
        if sequence is not None:
            raise MigrationError("canonical check-in store is active unmarked")
        facet_count = connection.execute("SELECT COUNT(*) FROM facets").fetchone()[0]
        seed_traits = connection.execute(
            """
            SELECT COUNT(*) FROM traits AS t
            JOIN facets AS f ON f.id = t.facet_id
            WHERE t.value_position IS NULL AND t.confidence = 0.0
              AND t.observation_count = 0 AND t.last_observation_at IS NULL
              AND t.last_synthesis_at IS NULL AND t.status = 'insufficient_data'
              AND t.notes IS NULL
            """
        ).fetchone()[0]
        trait_count = connection.execute("SELECT COUNT(*) FROM traits").fetchone()[0]
        if facet_count == 0 or trait_count != facet_count or seed_traits != facet_count:
            raise MigrationError("canonical check-in store is active unmarked")
    finally:
        connection.close()


def _import_checkin_db(
    source_db: Path,
    target_db: Path,
    source_subject_id: str,
    runtime_scope_id: str,
    original_policy_signature: str,
) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".relic.db.migrate.", dir=target_db.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    os.chmod(temporary_path, 0o600)
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = _connect_readonly(source_db, "source check-in database")
        target = sqlite3.connect(temporary_path)
        source.backup(target)
        _validate_checkin_connection(target, "source check-in database snapshot")
        target.execute("BEGIN IMMEDIATE")
        for table in _CHECKIN_SUBJECT_TABLES:
            unexpected = target.execute(
                f'SELECT DISTINCT subject_id FROM "{table}" WHERE subject_id != ?',
                (source_subject_id,),
            ).fetchone()
            if unexpected is not None:
                raise MigrationError("source check-in database contains a foreign subject scope")
        rows = target.execute("SELECT id, features_json FROM checkin_features").fetchall()
        for feature_id, raw_features in rows:
            try:
                features = json.loads(raw_features)
            except (TypeError, json.JSONDecodeError) as exc:
                raise MigrationError("source check-in features have an invalid schema") from exc
            if not isinstance(features, dict):
                raise MigrationError("source check-in features have an invalid schema")
            if "subject_id" in features:
                if features["subject_id"] != source_subject_id:
                    raise MigrationError("source check-in features contain a foreign subject scope")
                features["subject_id"] = runtime_scope_id
                target.execute(
                    "UPDATE checkin_features SET features_json = ? WHERE id = ?",
                    (json.dumps(features, ensure_ascii=False), feature_id),
                )
        for table in _CHECKIN_SUBJECT_TABLES:
            target.execute(
                f'UPDATE "{table}" SET subject_id = ? WHERE subject_id = ?',
                (runtime_scope_id, source_subject_id),
            )
        target.execute(
            "UPDATE checkin_cadence_state SET non_response_streak = 0, "
            "followup_non_response_streak = 0, diegetic_non_response_streak = 0"
        )
        target.execute(
            f"""
            CREATE TABLE {_CHECKIN_MIGRATION_TABLE} (
                migration_id TEXT PRIMARY KEY,
                source_subject_id TEXT NOT NULL,
                runtime_scope_id TEXT NOT NULL,
                original_policy_signature TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('db_imported', 'complete'))
            )
            """
        )
        target.execute(
            f"INSERT INTO {_CHECKIN_MIGRATION_TABLE} VALUES (?, ?, ?, ?, 'db_imported')",
            (
                _CHECKIN_MIGRATION_ID,
                source_subject_id,
                runtime_scope_id,
                original_policy_signature,
            ),
        )
        target.commit()
        target.execute("PRAGMA journal_mode = DELETE")
        target.close()
        target = None
        source.close()
        source = None
        with temporary_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_path, target_db)
        target_db.chmod(0o600)
        _fsync_directory(target_db.parent)
    except (MigrationError, OSError, sqlite3.Error) as exc:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("source check-in database import failed") from exc


def _write_checkin_policy(path: Path, payload: dict[str, bool]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".delivery-policy.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _complete_checkin_migration(
    target_db: Path, source_subject_id: str, runtime_scope_id: str
) -> None:
    connection = sqlite3.connect(target_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            f"UPDATE {_CHECKIN_MIGRATION_TABLE} SET state = 'complete' "
            "WHERE migration_id = ? AND source_subject_id = ? "
            "AND runtime_scope_id = ? AND state = 'db_imported'",
            (_CHECKIN_MIGRATION_ID, source_subject_id, runtime_scope_id),
        )
        if cursor.rowcount != 1:
            raise MigrationError("canonical check-in migration marker is invalid")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_legacy_subject(
    source_subject: Path,
    *,
    target_relic_home: Path,
    target_hermes_home: Path,
    private_env: Mapping[str, str] | None = None,
    expected_subject_id: str = "testsubject",
    dry_run: bool = False,
) -> MigrationResult:
    """Transform one legacy subject into an isolated canonical User.

    The source is read-only. The target must not already contain canonical User
    state. Credentials are accepted explicitly and only selected names are
    written to the target Hermes profile's private environment.
    """
    source_subject = Path(source_subject).expanduser().resolve()
    target_relic_home = Path(target_relic_home).expanduser().resolve()
    target_hermes_home = Path(target_hermes_home).expanduser().resolve()
    subject_id = _subject_id(source_subject)
    if subject_id != expected_subject_id:
        raise MigrationError(f"migration is subject-scoped and expected {expected_subject_id!r}")
    _assert_target_safe(target_relic_home, target_hermes_home)
    source = _read_source(source_subject)
    env = _validate_private_env(private_env or {})
    if dry_run:
        user = UserProfile(
            runtime_scope_id=generate_runtime_scope_id(),
            hermes_profile_name=HERMES_USER_PROFILE_NAME,
            user_home=target_relic_home / "user",
        )
        artifacts = _build_artifacts(source, user, env)
        continuity = _read_confirmed_continuity(
            source_subject / "continuity.db", subject_id, user.runtime_scope_id
        )
        return MigrationResult(
            subject_id=subject_id,
            target_relic_home=target_relic_home,
            target_hermes_home=target_hermes_home,
            dry_run=True,
            continuity_markers=len(continuity),
            delivery_armed=artifacts["runtime_policy"]["delivery_execution_armed"],
        )

    target_relic_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_hermes_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_relic_home.chmod(0o700)
    target_hermes_home.chmod(0o700)
    (target_hermes_home / "profiles").mkdir(mode=0o700, exist_ok=True)
    (target_hermes_home / "profiles").chmod(0o700)
    registry = ProfileRegistry(
        relic_home=target_relic_home,
        hermes_profiles_home=target_hermes_home / "profiles",
    )
    user = registry.initialize_user()
    artifacts = _build_artifacts(source, user, env)
    continuity = _read_confirmed_continuity(
        source_subject / "continuity.db", subject_id, user.runtime_scope_id
    )
    registry.materialize_user_artifacts(
        user,
        baseline=artifacts["baseline_response"]["baseline"],
        gumi_profile={
            key: value for key, value in artifacts["gumi_profile"].items()
            if key not in {"schema_version", "runtime_scope_id"}
        },
        runtime_policy={
            key: value for key, value in artifacts["runtime_policy"].items()
            if key not in {"schema_version", "runtime_scope_id"}
        },
        checkin_state={
            key: value for key, value in artifacts["checkin_state"].items()
            if key not in {"schema_version", "runtime_scope_id"}
        },
        private_env=env,
    )
    _write_continuity(target_relic_home / "user" / "continuity.json", continuity)
    _append_migration_provenance(registry, source_subject, source)
    return MigrationResult(
        subject_id=subject_id,
        target_relic_home=target_relic_home,
        target_hermes_home=target_hermes_home,
        dry_run=False,
        continuity_markers=len(continuity),
        delivery_armed=artifacts["runtime_policy"]["delivery_execution_armed"],
    )


def read_private_env(path: Path) -> dict[str, str]:
    """Reject the legacy credential-file route without opening the supplied path."""
    from relic.secrets import credential_setup_commands

    raise MigrationError(
        "Credential-file migration is not supported. Configure credentials privately.\n"
        + credential_setup_commands()
        + "\nThen rerun /gumi-setup."
    )


def _validate_private_env(values: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for name, value in values.items():
        if name not in _ALLOWED_PRIVATE_ENV:
            raise MigrationError(f"private credential is not approved for migration: {name}")
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise MigrationError(f"invalid private credential: {name}")
        clean[name] = value
    if "TELEGRAM_BOT_TOKEN" in clean and not _TELEGRAM_TOKEN.fullmatch(clean["TELEGRAM_BOT_TOKEN"]):
        raise MigrationError("invalid Telegram bot token")
    if "TELEGRAM_HOME_CHANNEL" in clean and not _TELEGRAM_TARGET.fullmatch(clean["TELEGRAM_HOME_CHANNEL"]):
        raise MigrationError("invalid Telegram home channel")
    return clean


def _subject_id(source_subject: Path) -> str:
    profile = source_subject / "subject_profile.json"
    if not profile.is_file():
        raise MigrationError("legacy subject_profile.json is required")
    try:
        value = json.loads(profile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError("legacy subject profile is unreadable") from exc
    subject_id = value.get("subject_id") if isinstance(value, dict) else None
    if not isinstance(subject_id, str) or not subject_id:
        raise MigrationError("legacy subject profile has no subject id")
    return subject_id


def _read_source(source_subject: Path) -> dict[str, dict[str, Any]]:
    names = (
        "subject_profile.json",
        "subject_baseline.json",
        "baseline_user_profile.json",
        "gumi_background_profile.json",
        "delivery_policy.json",
        "boundary_policy.json",
        "gumi_generation_constraints.json",
    )
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        path = source_subject / name
        if not path.is_file():
            result[name] = {}
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"legacy artifact is unreadable: {name}") from exc
        if not isinstance(data, dict):
            raise MigrationError(f"legacy artifact has invalid schema: {name}")
        result[name] = data
    return result


def _build_artifacts(
    source: Mapping[str, Mapping[str, Any]], user: UserProfile, private_env: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    baseline = source.get("subject_baseline.json", {})
    user_profile = source.get("baseline_user_profile.json", {})
    background = source.get("gumi_background_profile.json", {})
    policy = source.get("delivery_policy.json", {})
    boundaries = source.get("boundary_policy.json", {})
    constraints = source.get("gumi_generation_constraints.json", {})
    quiet = policy.get("quiet_hours") if isinstance(policy.get("quiet_hours"), dict) else {}
    delivery_enabled = policy.get("delivery_enabled") is True
    telegram_ready = {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_HOME_CHANNEL",
    }.issubset(private_env)
    armed = delivery_enabled and telegram_ready
    media = {
        "images": {
            "selected": boundaries.get("image_allowed") is True,
            "enabled": boundaries.get("image_allowed") is True and "GEMINI_API_KEY" in private_env,
        },
        "audio": {
            "selected": boundaries.get("audio_allowed") is True,
            "enabled": boundaries.get("audio_allowed") is True,
        },
        "music": {
            "selected": boundaries.get("music_allowed") is True,
            "enabled": boundaries.get("music_allowed") is True,
        },
    }
    references = {
        name: {
            "location": "hermes_profile:.env",
            "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
        for name, value in private_env.items()
        if name in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"}
    }
    return {
        "baseline_response": {
            "schema_version": 1,
            "runtime_scope_id": user.runtime_scope_id,
            "baseline": {
                "timezone": quiet.get("timezone", "UTC"),
                "interaction": _clean(baseline.get("interaction", {})),
                "psychological": _clean(baseline.get("psychological", {})),
                "extended_facets": _clean(baseline.get("extended_facets", {})),
                "preferences": _clean(user_profile.get("interaction_preferences", {})),
                "relational": _clean(user_profile.get("relational_expectations", {})),
                "boundaries": _clean(user_profile.get("boundaries", {})),
                "self_report": _clean(user_profile.get("self_report_fields", {})),
            },
        },
        "gumi_profile": {
            "schema_version": 1,
            "runtime_scope_id": user.runtime_scope_id,
            "flavor": {
                "id": "gumi-classic",
                "version": 1,
                "agent_name": _clean_scalar(background.get("agent_name", "Gumi")),
            },
            "setup_mode": "legacy_import",
            "preferences": {
                "interaction": _clean(user_profile.get("interaction_preferences", {})),
                "relational": _clean(background.get("domains", {}).get("relationship_stance", {}))
                if isinstance(background.get("domains"), dict)
                else {},
                "safety": _clean({**boundaries, **constraints.get("boundaries", {})}),
                "proactivity": {
                    "recurring_messages": "on" if delivery_enabled else "off_until_armed",
                    "after_no_reply": "pause",
                    "quiet_hours": f"{quiet.get('start', '22:00')}-{quiet.get('end', '08:00')}",
                    "maximum_frequency": policy.get("maximum_contact_frequency", "2/day"),
                },
            },
            "media": media,
        },
        "runtime_policy": {
            "schema_version": 1,
            "runtime_scope_id": user.runtime_scope_id,
            "delivery_capability_enabled": armed,
            "delivery_execution_armed": armed,
            "first_message": "wait",
            "credential_references": references,
        },
        "checkin_state": {
            "schema_version": 1,
            "runtime_scope_id": user.runtime_scope_id,
            "recurring": {"status": "armed" if armed else "off"},
            "first_message": {"status": "waiting"},
        },
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            lowered = key.lower()
            if any(term in lowered for term in _FORBIDDEN_KEY_TERMS | _INTERNAL_KEY_TERMS):
                continue
            cleaned[key] = _clean(item)
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _clean_scalar(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) else "Gumi"


def _read_confirmed_continuity(path: Path, subject_id: str, scope_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT marker_id, source_type, created_at, subject_words_json,
                   gumi_agreed_words_json, status, gumi_recall_allowed,
                   recall_count, expires_at
            FROM continuity_marker
            WHERE subject_id = ? AND subject_confirmation = 1
              AND gumi_recall_allowed = 1 AND status = 'active'
            ORDER BY created_at
            """,
            (subject_id,),
        ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    markers: list[dict[str, Any]] = []
    for row in rows:
        try:
            subject_words = json.loads(row["subject_words_json"] or "[]")
            gumi_words = json.loads(row["gumi_agreed_words_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(subject_words, list) or not isinstance(gumi_words, list):
            continue
        markers.append(
            {
                "marker_id": row["marker_id"],
                "hermes_profile_id": HERMES_USER_PROFILE_NAME,
                "runtime_scope_id": scope_id,
                "user_confirmation": True,
                "subject_words": [str(item) for item in subject_words],
                "gumi_words": [str(item) for item in gumi_words],
                "source_type": row["source_type"],
                "created_at": row["created_at"],
                "recall_count": int(row["recall_count"] or 0),
                "expires_at": row["expires_at"],
            }
        )
    return markers


def _write_continuity(path: Path, markers: list[dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"markers": markers}, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def _append_migration_provenance(
    registry: ProfileRegistry, source_subject: Path, source: Mapping[str, Mapping[str, Any]]
) -> None:
    record = {
        "schema_version": 1,
        "event": "legacy_subject_migrated",
        "source": "legacy_subject_profile",
        "source_artifacts": {
            name: hashlib.sha256((source_subject / name).read_bytes()).hexdigest()
            for name in source
            if (source_subject / name).is_file()
        },
    }
    with registry.onboarding_provenance_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    registry.onboarding_provenance_path.chmod(0o600)


def _assert_target_safe(relic_home: Path, hermes_home: Path) -> None:
    user_home = relic_home / "user"
    if user_home.exists() and any(user_home.iterdir()):
        raise MigrationError("target is not empty: canonical RELIC User already exists")
    profile_home = hermes_home / "profiles" / HERMES_USER_PROFILE_NAME
    if profile_home.exists() and any(profile_home.iterdir()):
        raise MigrationError("target is not empty: canonical Hermes profile already exists")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relic.profile.migration")
    parser.add_argument("--source-subject", type=Path, required=True)
    parser.add_argument("--target-relic-home", type=Path, required=True)
    parser.add_argument("--target-hermes-home", type=Path, required=True)
    parser.add_argument("--private-env-file", type=Path)
    parser.add_argument("--expected-subject-id", default="testsubject")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        private_env = read_private_env(args.private_env_file) if args.private_env_file else {}
        result = migrate_legacy_subject(
            args.source_subject,
            target_relic_home=args.target_relic_home,
            target_hermes_home=args.target_hermes_home,
            private_env=private_env,
            expected_subject_id=args.expected_subject_id,
            dry_run=args.dry_run,
        )
    except (MigrationError, OSError) as exc:
        print(f"relic-gumi migration blocked: {exc}", file=sys.stderr)
        return 1
    mode = "dry-run" if result.dry_run else "imported"
    print(f"relic-gumi migration {mode}: subject={result.subject_id}")
    print(f"targets: RELIC_HOME={result.target_relic_home} HERMES_HOME={result.target_hermes_home}")
    print(f"continuity markers: {result.continuity_markers}")
    print(f"delivery: {'armed' if result.delivery_armed else 'blocked pending credentials'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
