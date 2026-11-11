from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")

import os
from io import StringIO
from pathlib import Path


from relic.profile import user_onboarding
from relic.profile.registry import MultipleLegacyProfilesError, ProfileRegistry
from tests.secret_fakes import onboard_with_scoped_credentials
from relic.profile.user_onboarding import UserOnboarding, load_onboarding_state


_VALID_GEMINI_KEY = "AIza" + "a" * 35
_VALID_TELEGRAM_TOKEN = "123456789:" + "a" * 25
_VALID_TELEGRAM_TARGET = "1234567890"


def _registry(tmp_path: Path) -> ProfileRegistry:
    return ProfileRegistry(
        relic_home=tmp_path / "relic",
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )


def _input(*answers: str) -> StringIO:
    return StringIO("\n".join(answers) + "\n")


def _skip_battery(monkeypatch) -> None:
    monkeypatch.setattr(
        user_onboarding,
        "collect_user_baseline_battery",
        lambda *_args, **_kwargs: {"responses": {}},
    )


def test_eof_cancels_without_onboarding_persistence_after_user_initialization(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    result = onboard_with_scoped_credentials(registry, io_in=StringIO(), io_out=StringIO(), environ={}).run()

    assert result is None
    assert registry.load_user() is not None
    assert not registry.gumi_profile_path.exists()


def test_quoted_environment_gemini_key_is_normalized_before_injected_validation(
    tmp_path: Path, monkeypatch
) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    seen: list[str] = []

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "y", "n", "n", "", "", "w"),
        io_out=StringIO(),
        environ={"GEMINI_API_KEY": f'"{_VALID_GEMINI_KEY}"'},
        gemini_validator=lambda key: seen.append(key) is None,
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert seen == [_VALID_GEMINI_KEY]
    assert state is not None
    assert state["media"]["images"]["enabled"] is True
    assert not registry.hermes_user_env_path.exists()


def test_default_gemini_validation_rejects_placeholder_key(tmp_path: Path, monkeypatch) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "y", "n", "n", "", "", "w"),
        io_out=StringIO(),
        environ={"GEMINI_API_KEY": '"placeholder"'},
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert state is not None
    assert state["media"]["images"] == {
        "selected": True,
        "enabled": False,
        "remediation": "Add a Gemini API key with hermes --profile relic-gumi config set GEMINI_API_KEY.",
    }
    assert not registry.hermes_user_env_path.exists()


def test_injected_gemini_validator_can_reject_a_structurally_valid_key(
    tmp_path: Path, monkeypatch
) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    seen: list[str] = []

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "y", "n", "n", "", "", "w"),
        io_out=StringIO(),
        environ={"GEMINI_API_KEY": _VALID_GEMINI_KEY},
        gemini_validator=lambda key: seen.append(key) is None and False,
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert seen == [_VALID_GEMINI_KEY]
    assert state is not None
    assert state["media"]["images"]["enabled"] is False
    assert not registry.hermes_user_env_path.exists()


class _PromptForbidden:
    def readline(self) -> str:
        raise AssertionError("onboarding prompted before legacy validation")


def test_multiple_legacy_profiles_are_rejected_before_first_prompt(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for name in ("first", "second"):
        profile = registry.subjects_dir / name
        profile.mkdir(parents=True)
        (profile / "subject_profile.json").write_text("{}", encoding="utf-8")

    with pytest.raises(MultipleLegacyProfilesError):
        onboard_with_scoped_credentials(registry, io_in=_PromptForbidden(), io_out=StringIO(), environ={}).run()


@pytest.mark.parametrize("source", ["environ", "env_file"])
def test_discovered_ambient_telegram_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    environ: dict[str, str] = {}
    if source == "environ":
        environ = {
            "TELEGRAM_BOT_TOKEN": _VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": _VALID_TELEGRAM_TARGET,
        }
    else:
        registry.hermes_user_home.mkdir(parents=True)
        (registry.hermes_user_home / ".env").write_text(
            "\n".join(
                (
                    f"TELEGRAM_BOT_TOKEN={_VALID_TELEGRAM_TOKEN}",
                    f"TELEGRAM_HOME_CHANNEL={_VALID_TELEGRAM_TARGET}",
                )
            ),
            encoding="utf-8",
        )
    output = StringIO()

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "n", "n", "n", "n", "w"),
        io_out=output,
        environ=environ,
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert state is not None
    assert state["delivery_capability_enabled"] is False
    assert _VALID_TELEGRAM_TOKEN not in output.getvalue()
    assert _VALID_TELEGRAM_TARGET not in output.getvalue()
    if source == "environ":
        assert _VALID_TELEGRAM_TARGET[-4:] in output.getvalue()
    else:
        assert "hermes --profile relic-gumi config set TELEGRAM_BOT_TOKEN" in output.getvalue()


def test_confirmed_ambient_telegram_enables_capability_without_printing_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    output = StringIO()

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "n", "n", "n", "y", "n", "w"),
        io_out=output,
        environ={
            "TELEGRAM_BOT_TOKEN": _VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": _VALID_TELEGRAM_TARGET,
        },
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert state is not None
    assert state["delivery_capability_enabled"] is True
    assert state["delivery_execution_armed"] is False
    assert not registry.hermes_user_env_path.exists()
    assert registry.load_user_private_env()["TELEGRAM_BOT_TOKEN"] == _VALID_TELEGRAM_TOKEN
    assert registry.load_user_private_env()["TELEGRAM_HOME_CHANNEL"] == _VALID_TELEGRAM_TARGET
    assert _VALID_TELEGRAM_TOKEN not in output.getvalue()
    assert _VALID_TELEGRAM_TARGET not in output.getvalue()


def test_load_onboarding_state_returns_valid_canonical_state(tmp_path: Path, monkeypatch) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)

    result = onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "n", "n", "n", "w"),
        io_out=StringIO(),
        environ={},
    ).run()

    assert result is not None
    state = load_onboarding_state(registry.user_home)
    assert state is not None
    assert state["runtime_scope_id"] == result.user.runtime_scope_id
    assert state["first_message"] == "wait"


def test_load_onboarding_state_returns_none_when_missing(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    user_home.mkdir(mode=0o700)

    assert load_onboarding_state(user_home) is None


def test_load_onboarding_state_rejects_incomplete_artifact_set(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.initialize_user()
    registry.baseline_response_path.write_text("{}", encoding="utf-8")
    registry.baseline_response_path.chmod(0o600)

    with pytest.raises(ValueError, match="incomplete"):
        load_onboarding_state(registry.user_home)


def test_load_onboarding_state_repairs_private_file_permissions(
    tmp_path: Path, monkeypatch
) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "n", "n", "n", "", "", "w"),
        io_out=StringIO(),
        environ={},
    ).run()
    registry.gumi_profile_path.chmod(0o644)

    assert load_onboarding_state(registry.user_home) is not None
    assert registry.gumi_profile_path.stat().st_mode & 0o777 == 0o600


def test_load_onboarding_state_rejects_symlink(tmp_path: Path, monkeypatch) -> None:
    _skip_battery(monkeypatch)
    registry = _registry(tmp_path)
    onboard_with_scoped_credentials(
        registry,
        io_in=_input("d", "c", "n", "n", "n", "", "", "w"),
        io_out=StringIO(),
        environ={},
    ).run()
    target = tmp_path / "outside.json"
    target.write_text(registry.gumi_profile_path.read_text(encoding="utf-8"), encoding="utf-8")
    registry.gumi_profile_path.unlink()
    registry.gumi_profile_path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        load_onboarding_state(registry.user_home)
