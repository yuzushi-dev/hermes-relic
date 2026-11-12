"""Profile registry with canonical singleton User state."""

from __future__ import annotations

import json
import os
import hashlib
import logging
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from relic.paths import get_relic_home
from relic.hermes_runtime import (
    HERMES_USER_PROFILE_NAME,
    generate_runtime_scope_id,
    validate_runtime_scope_id,
)

logger = logging.getLogger(__name__)

USER_PROFILE_FILENAME = "user_profile.json"
BASELINE_RESPONSE_FILENAME = "baseline_response.json"
GUMI_PROFILE_FILENAME = "gumi_profile.json"
RUNTIME_POLICY_FILENAME = "runtime_policy.json"
CHECKIN_STATE_FILENAME = "checkin_state.json"
PROVENANCE_DIRNAME = "provenance"
ONBOARDING_PROVENANCE_FILENAME = "onboarding.jsonl"
USER_PROFILE_SCHEMA_VERSION = 1
USER_ARTIFACT_SCHEMA_VERSION = 1
_LEGACY_USER_METADATA_FILENAME = "metadata.json"
_LEGACY_USER_ONBOARDING_FILENAME = "onboarding.json"
_FORBIDDEN_USER_ARTIFACT_FIELDS = frozenset(
    {"subject", "experiment", "researcher", "consent", "emergency", "risk"}
)


class LegacyProfileState(str, Enum):
    FRESH_INSTALL = "fresh_install"
    SINGLE_LEGACY_PROFILE = "single_legacy_profile"
    MULTIPLE_LEGACY_PROFILES = "multiple_legacy_profiles"


@dataclass(frozen=True)
class LegacyProfileDetection:
    state: LegacyProfileState
    profile_names: tuple[str, ...]


class LegacyProfileMigrationRequired(RuntimeError):
    """A legacy profile requires an explicit migration decision."""


class MultipleLegacyProfilesError(LegacyProfileMigrationRequired):
    """Multiple legacy profiles cannot be merged into the singleton User."""


@dataclass(frozen=True)
class UserProfile:
    """Canonical local User state."""

    runtime_scope_id: str
    hermes_profile_name: str
    user_home: Path

    def __post_init__(self) -> None:
        validate_runtime_scope_id(self.runtime_scope_id)
        if self.hermes_profile_name != HERMES_USER_PROFILE_NAME:
            raise ValueError(f"hermes_profile_name must be {HERMES_USER_PROFILE_NAME!r}")

    def to_artifact(self) -> dict[str, Any]:
        return {
            "schema_version": USER_PROFILE_SCHEMA_VERSION,
            "runtime_scope_id": self.runtime_scope_id,
            "hermes_profile_name": self.hermes_profile_name,
        }

    @classmethod
    def from_artifact(cls, data: Any, *, user_home: Path) -> "UserProfile":
        expected_fields = {
            "schema_version",
            "runtime_scope_id",
            "hermes_profile_name",
        }
        if not isinstance(data, dict) or set(data) != expected_fields:
            raise ValueError("User profile has an invalid schema")
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != USER_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("User profile has an unsupported schema_version")
        if not isinstance(data["hermes_profile_name"], str):
            raise ValueError("User profile has an invalid hermes_profile_name")
        return cls(
            runtime_scope_id=validate_runtime_scope_id(data["runtime_scope_id"]),
            hermes_profile_name=data["hermes_profile_name"],
            user_home=user_home,
        )

def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)




class ProfileRegistry:
    def __init__(
        self,
        relic_home: Optional[Path] = None,
        hermes_profiles_home: Optional[Path] = None,
    ) -> None:
        if relic_home is None:
            relic_home = get_relic_home()
        if hermes_profiles_home is None:
            env_home = os.environ.get("HERMES_PROFILES_HOME")
            hermes_profiles_home = (
                Path(env_home) if env_home else Path.home() / ".hermes" / "profiles"
            )
        self.relic_home = relic_home
        self.hermes_profiles_home = hermes_profiles_home
        self.user_home = relic_home / "user"
        self.user_profile_path = self.user_home / USER_PROFILE_FILENAME
        self.baseline_response_path = self.user_home / BASELINE_RESPONSE_FILENAME
        self.gumi_profile_path = self.user_home / GUMI_PROFILE_FILENAME
        self.runtime_policy_path = self.user_home / RUNTIME_POLICY_FILENAME
        self.checkin_state_path = self.user_home / CHECKIN_STATE_FILENAME
        self.provenance_dir = self.user_home / PROVENANCE_DIRNAME
        self.onboarding_provenance_path = self.provenance_dir / ONBOARDING_PROVENANCE_FILENAME
        self.legacy_user_metadata_path = self.user_home / _LEGACY_USER_METADATA_FILENAME
        self.legacy_user_onboarding_path = self.user_home / _LEGACY_USER_ONBOARDING_FILENAME
        self.hermes_user_home = hermes_profiles_home / HERMES_USER_PROFILE_NAME
        self.hermes_user_env_path = self.hermes_user_home / ".env"
        self.subjects_dir = relic_home / "subjects"

    def _ensure_user_home(self) -> None:
        if self.user_home.is_symlink():
            raise ValueError("canonical User home must not be a symlink")
        self.user_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.user_home.is_dir():
            raise ValueError("canonical User home is not a directory")
        if os.name == "posix":
            self.user_home.chmod(0o700)

    def _ensure_hermes_user_home(self) -> None:
        if self.hermes_user_home.is_symlink():
            raise ValueError("canonical Hermes profile must not be a symlink")
        self.hermes_user_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.hermes_user_home.is_dir():
            raise ValueError("canonical Hermes profile is not a directory")
        if os.name == "posix":
            self.hermes_user_home.chmod(0o700)

    def _require_private_file(self, path: Path, label: str) -> None:
        if path.is_symlink():
            raise ValueError(f"{label} must not be a symlink")
        if not path.is_file():
            raise ValueError(f"{label} is not a file")
        if os.name == "posix":
            path.chmod(0o600)

    def load_user(self) -> UserProfile | None:
        """Load the canonical User without creating or migrating state."""
        if self.user_profile_path.is_symlink():
            raise ValueError("canonical User profile must not be a symlink")
        if not self.user_profile_path.exists():
            return None
        self._require_private_file(self.user_profile_path, "canonical User profile")
        self._ensure_user_home()
        try:
            data = _read_json(self.user_profile_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("canonical User profile is unreadable") from exc
        return UserProfile.from_artifact(data, user_home=self.user_home)

    def detect_legacy_profiles(self) -> LegacyProfileDetection:
        """Classify legacy profile storage without reading or merging it."""
        profile_names = tuple(
            entry.name
            for entry in sorted(self.subjects_dir.iterdir())
            if entry.is_dir() and (entry / "subject_profile.json").is_file()
        ) if self.subjects_dir.is_dir() else ()
        if not profile_names:
            state = LegacyProfileState.FRESH_INSTALL
        elif len(profile_names) == 1:
            state = LegacyProfileState.SINGLE_LEGACY_PROFILE
        else:
            state = LegacyProfileState.MULTIPLE_LEGACY_PROFILES
        return LegacyProfileDetection(state=state, profile_names=profile_names)

    def _write_user_profile_once(self, user: UserProfile) -> bool:
        content = json.dumps(user.to_artifact(), indent=2, ensure_ascii=False) + "\n"
        try:
            descriptor = os.open(
                self.user_profile_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as profile_file:
            profile_file.write(content)
            profile_file.flush()
            os.fsync(profile_file.fileno())
        if os.name == "posix":
            self.user_profile_path.chmod(0o600)
        return True

    def _migrate_legacy_user_profile(self) -> UserProfile | None:
        path = self.legacy_user_metadata_path
        if not path.exists():
            return None
        self._require_private_file(path, "legacy User profile")
        try:
            user = UserProfile.from_artifact(_read_json(path), user_home=self.user_home)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("legacy User profile is unreadable") from exc
        os.replace(path, self.user_profile_path)
        if os.name == "posix":
            self.user_profile_path.chmod(0o600)
        return user

    def initialize_user(self) -> UserProfile:
        """Create or load the one canonical User for this installation."""
        existing = self.load_user()
        if existing is not None:
            return existing

        legacy = self.detect_legacy_profiles()
        if legacy.state is LegacyProfileState.SINGLE_LEGACY_PROFILE:
            raise LegacyProfileMigrationRequired(
                f"Legacy profile {legacy.profile_names[0]!r} requires explicit migration"
            )
        if legacy.state is LegacyProfileState.MULTIPLE_LEGACY_PROFILES:
            names = ", ".join(legacy.profile_names)
            raise MultipleLegacyProfilesError(
                "Multiple legacy profiles detected "
                f"({names}); cannot initialize the canonical User without explicit migration"
            )

        self._ensure_user_home()
        migrated = self._migrate_legacy_user_profile()
        if migrated is not None:
            return migrated
        user = UserProfile(
            runtime_scope_id=generate_runtime_scope_id(),
            hermes_profile_name=HERMES_USER_PROFILE_NAME,
            user_home=self.user_home,
        )
        if self._write_user_profile_once(user):
            return user
        existing = self.load_user()
        if existing is None:
            raise RuntimeError("canonical User profile disappeared during initialization")
        return existing

    def _artifact_paths(self) -> dict[str, Path]:
        return {
            "user_profile": self.user_profile_path,
            "baseline_response": self.baseline_response_path,
            "gumi_profile": self.gumi_profile_path,
            "runtime_policy": self.runtime_policy_path,
            "checkin_state": self.checkin_state_path,
        }

    @staticmethod
    def _artifact_text(data: Mapping[str, Any]) -> str:
        return json.dumps(dict(data), indent=2, ensure_ascii=False) + "\n"

    @staticmethod
    def _artifact_fingerprint(data: Mapping[str, Any]) -> str:
        content = json.dumps(dict(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _forbidden_artifact_fields(data: Any) -> set[str]:
        if isinstance(data, Mapping):
            found = {
                key
                for key in data
                if isinstance(key, str)
                and any(term in key.lower() for term in _FORBIDDEN_USER_ARTIFACT_FIELDS)
            }
            for value in data.values():
                found.update(ProfileRegistry._forbidden_artifact_fields(value))
            return found
        if isinstance(data, list):
            return set().union(*(ProfileRegistry._forbidden_artifact_fields(item) for item in data))
        return set()

    @staticmethod
    def _expected_artifact_fields(name: str) -> set[str]:
        return {
            "user_profile": {"schema_version", "runtime_scope_id", "hermes_profile_name"},
            "baseline_response": {"schema_version", "runtime_scope_id", "baseline"},
            "gumi_profile": {
                "schema_version",
                "runtime_scope_id",
                "flavor",
                "setup_mode",
                "preferences",
                "media",
            },
            "runtime_policy": {
                "schema_version",
                "runtime_scope_id",
                "delivery_capability_enabled",
                "delivery_execution_armed",
                "first_message",
                "credential_references",
            },
            "checkin_state": {"schema_version", "runtime_scope_id", "recurring", "first_message"},
        }[name]

    def _validate_artifact(
        self, name: str, data: Mapping[str, Any], user: UserProfile
    ) -> dict[str, Any]:
        artifact = dict(data)
        if set(artifact) != self._expected_artifact_fields(name):
            raise ValueError(f"{name} has an invalid schema")
        if artifact.get("schema_version") != USER_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"{name} has an unsupported schema_version")
        if artifact.get("runtime_scope_id") != user.runtime_scope_id:
            raise ValueError(f"{name} runtime_scope_id does not match the canonical User")
        forbidden = self._forbidden_artifact_fields(artifact)
        if forbidden:
            raise ValueError(f"{name} contains forbidden fields: {', '.join(sorted(forbidden))}")
        return artifact

    def load_user_private_env(self) -> dict[str, str]:
        """Hermes-resolved snapshot, not credentials for this registry's path.

        A registry path does not establish a Hermes profile scope. Call only
        inside the target Hermes runtime, not to validate a CLI target profile.
        Never read credential files here.
        """
        from relic.secrets import scoped_credentials

        return scoped_credentials()

    @staticmethod
    def _stage_private_file(path: Path, text: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        staged = Path(name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return staged

    @staticmethod
    def _backup_path(path: Path) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
        os.close(descriptor)
        return Path(name)

    def _replace_private_files(
        self, replacements: Mapping[Path, str], removals: tuple[Path, ...] = ()
    ) -> None:
        staged: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        installed: list[Path] = []
        try:
            for path, text in replacements.items():
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError(f"canonical private artifact is unsafe: {path}")
                staged[path] = self._stage_private_file(path, text)
            for path in removals:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError(f"legacy private artifact is unsafe: {path}")
            for path, stage in staged.items():
                if path.exists():
                    backup = self._backup_path(path)
                    os.replace(path, backup)
                    backups[path] = backup
                os.replace(stage, path)
                installed.append(path)
            for path in removals:
                if path.exists():
                    backup = self._backup_path(path)
                    os.replace(path, backup)
                    backups[path] = backup
            for parent in {path.parent for path in (*replacements, *removals)}:
                try:
                    descriptor = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError:
                    pass
        except BaseException:
            for path in reversed(installed):
                if path.exists() and not path.is_symlink():
                    path.unlink()
            for path, backup in backups.items():
                if backup.exists():
                    os.replace(backup, path)
            raise
        finally:
            for stage in staged.values():
                stage.unlink(missing_ok=True)
            for backup in backups.values():
                backup.unlink(missing_ok=True)

    def load_user_artifacts(self) -> dict[str, dict[str, Any]] | None:
        """Load the complete versioned User artifact set."""
        paths = self._artifact_paths()
        configured_paths = tuple(path for name, path in paths.items() if name != "user_profile")
        if not any(path.exists() for path in configured_paths):
            return None
        if not all(path.exists() for path in paths.values()):
            raise ValueError("canonical User artifacts are incomplete")
        loaded: dict[str, dict[str, Any]] = {}
        for name, path in paths.items():
            self._require_private_file(path, f"canonical {name}")
            try:
                value = _read_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"canonical {name} is unreadable") from exc
            if not isinstance(value, dict):
                raise ValueError(f"canonical {name} has an invalid schema")
            loaded[name] = value
        user = UserProfile.from_artifact(loaded["user_profile"], user_home=self.user_home)
        for name, data in loaded.items():
            self._validate_artifact(name, data, user)
        self._require_private_file(self.onboarding_provenance_path, "canonical provenance")
        return loaded

    def load_user_configuration(self) -> dict[str, Any] | None:
        artifacts = self.load_user_artifacts()
        if artifacts is None:
            return None
        gumi = artifacts["gumi_profile"]
        policy = artifacts["runtime_policy"]
        return {
            "runtime_scope_id": artifacts["user_profile"]["runtime_scope_id"],
            "baseline": artifacts["baseline_response"]["baseline"],
            "preferences": gumi["preferences"],
            "media": gumi["media"],
            "delivery_capability_enabled": policy["delivery_capability_enabled"],
            "delivery_execution_armed": policy["delivery_execution_armed"],
            "first_message": policy["first_message"],
            "credential_references": policy["credential_references"],
            "checkin_state": artifacts["checkin_state"],
        }

    def mark_first_message_sent(self) -> bool:
        """Move ``checkin_state.first_message.status`` from ``requested`` to ``sent``.

        Only that one field changes. The file is replaced atomically (0600) together
        with an appended provenance record. Returns False when nothing was requested.
        """
        artifacts = self.load_user_artifacts()
        if artifacts is None:
            return False
        state = artifacts["checkin_state"]
        first_message = state.get("first_message")
        if not isinstance(first_message, Mapping) or first_message.get("status") != "requested":
            return False
        user = UserProfile.from_artifact(artifacts["user_profile"], user_home=self.user_home)
        updated = self._validate_artifact(
            "checkin_state",
            {**state, "first_message": {**first_message, "status": "sent"}},
            user,
        )
        record = {
            "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
            "event": "first_message_sent",
            "runtime_scope_id": user.runtime_scope_id,
            "artifact_fingerprints": {"checkin_state": self._artifact_fingerprint(updated)},
        }
        provenance = self.onboarding_provenance_path.read_text(encoding="utf-8")
        if provenance and not provenance.endswith("\n"):
            provenance += "\n"
        self._replace_private_files(
            {
                self.checkin_state_path: self._artifact_text(updated),
                self.onboarding_provenance_path: provenance
                + json.dumps(record, sort_keys=True, ensure_ascii=False)
                + "\n",
            }
        )
        return True

    def materialize_user_artifacts(
        self,
        user: UserProfile,
        *,
        baseline: Mapping[str, Any],
        gumi_profile: Mapping[str, Any],
        runtime_policy: Mapping[str, Any],
        checkin_state: Mapping[str, Any],
        private_env: Mapping[str, str],
    ) -> Path:
        """Commit User artifacts and private Hermes credentials as one rollback-safe change."""
        persisted = self.initialize_user()
        if persisted != user:
            raise ValueError("canonical User changed during onboarding")
        self._ensure_user_home()
        self.provenance_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.provenance_dir.is_symlink() or not self.provenance_dir.is_dir():
            raise ValueError("canonical provenance directory is unsafe")
        if os.name == "posix":
            self.provenance_dir.chmod(0o700)
        self._ensure_hermes_user_home()

        artifacts = {
            "user_profile": user.to_artifact(),
            "baseline_response": {
                "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
                "runtime_scope_id": user.runtime_scope_id,
                "baseline": dict(baseline),
            },
            "gumi_profile": {
                "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
                "runtime_scope_id": user.runtime_scope_id,
                **dict(gumi_profile),
            },
            "runtime_policy": {
                "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
                "runtime_scope_id": user.runtime_scope_id,
                **dict(runtime_policy),
            },
            "checkin_state": {
                "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
                "runtime_scope_id": user.runtime_scope_id,
                **dict(checkin_state),
            },
        }
        validated = {
            name: self._validate_artifact(name, artifact, user)
            for name, artifact in artifacts.items()
        }
        existing_env = self.load_user_private_env()
        for name, value in private_env.items():
            if not isinstance(name, str) or not isinstance(value, str) or "\n" in value:
                raise ValueError("canonical Hermes environment contains an invalid value")
            existing_env[name] = value
        serialized = "\n".join(self._artifact_text(value) for value in validated.values())
        raw_private_values = {value for value in existing_env.values() if len(value) >= 8}
        if any(value in serialized for value in raw_private_values):
            raise ValueError("raw private values must not appear in Relic artifacts")
        provenance_record = {
            "schema_version": USER_ARTIFACT_SCHEMA_VERSION,
            "event": "configured",
            "runtime_scope_id": user.runtime_scope_id,
            "artifact_fingerprints": {
                name: self._artifact_fingerprint(artifact) for name, artifact in validated.items()
            },
        }
        existing_provenance = (
            self.onboarding_provenance_path.read_text(encoding="utf-8")
            if self.onboarding_provenance_path.exists()
            else ""
        )
        if existing_provenance and not existing_provenance.endswith("\n"):
            existing_provenance += "\n"
        replacements = {
            self._artifact_paths()[name]: self._artifact_text(artifact)
            for name, artifact in validated.items()
        }
        replacements[self.onboarding_provenance_path] = (
            existing_provenance
            + json.dumps(provenance_record, sort_keys=True, ensure_ascii=False)
            + "\n"
        )
        self._replace_private_files(replacements, (self.legacy_user_onboarding_path,))
        return self.user_profile_path

