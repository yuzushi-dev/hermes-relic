from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import stat
from pathlib import Path

import pytest

from relic.profile.registry import (
    LegacyProfileState,
    MultipleLegacyProfilesError,
    ProfileRegistry,
)


def _registry(tmp_path: Path) -> ProfileRegistry:
    return ProfileRegistry(
        relic_home=tmp_path / "relic",
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )


def _legacy_profile(relic_home: Path, name: str) -> None:
    profile = relic_home / "subjects" / name / "subject_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")


def test_fresh_install_creates_the_canonical_user_path(tmp_path: Path):
    registry = _registry(tmp_path)

    assert registry.detect_legacy_profiles().state is LegacyProfileState.FRESH_INSTALL
    user = registry.initialize_user()

    assert user.user_home == tmp_path / "relic" / "user"
    assert user.hermes_profile_name == HERMES_USER_PROFILE_NAME
    assert registry.user_profile_path == user.user_home / "user_profile.json"
    assert not registry.subjects_dir.exists()
    assert stat.S_IMODE(user.user_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(registry.user_profile_path.stat().st_mode) == 0o600


def test_initialize_user_reuses_the_persisted_runtime_scope(tmp_path: Path):
    first = _registry(tmp_path).initialize_user()
    second = _registry(tmp_path).initialize_user()

    assert second.runtime_scope_id == first.runtime_scope_id
    assert _registry(tmp_path).load_user() == second


def test_load_user_rejects_an_invalid_runtime_scope(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.user_home.mkdir(parents=True)
    registry.user_profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_scope_id": "not-an-opaque-runtime-scope",
                "hermes_profile_name": HERMES_USER_PROFILE_NAME,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime_scope_id"):
        registry.load_user()


def test_legacy_detection_reports_one_profile_without_initializing_a_user(tmp_path: Path):
    registry = _registry(tmp_path)
    _legacy_profile(registry.relic_home, "legacy-a")

    detection = registry.detect_legacy_profiles()

    assert detection.state is LegacyProfileState.SINGLE_LEGACY_PROFILE
    assert detection.profile_names == ("legacy-a",)
    with pytest.raises(RuntimeError, match="explicit migration"):
        registry.initialize_user()
    assert registry.load_user() is None


def test_multiple_legacy_profiles_hard_stop_in_stable_order(tmp_path: Path):
    registry = _registry(tmp_path)
    _legacy_profile(registry.relic_home, "legacy-b")
    _legacy_profile(registry.relic_home, "legacy-a")

    detection = registry.detect_legacy_profiles()

    assert detection.state is LegacyProfileState.MULTIPLE_LEGACY_PROFILES
    assert detection.profile_names == ("legacy-a", "legacy-b")
    with pytest.raises(MultipleLegacyProfilesError, match="legacy-a, legacy-b"):
        registry.initialize_user()
    assert registry.load_user() is None
