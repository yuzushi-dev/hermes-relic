from __future__ import annotations

import pytest
from tests.secret_fakes import seed_credentials
pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import relic.gumi_install as cli


def test_cli_passes_noninteractive_install_options(monkeypatch, tmp_path, capsys):
    calls: list[dict[str, object]] = []

    class Installer:
        def install(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(profile_home=tmp_path / "profiles" / HERMES_USER_PROFILE_NAME)

    monkeypatch.setattr(cli, "Installer", Installer)

    assert cli.main(["install", "--hermes-home", str(tmp_path), "--non-interactive"]) == 0
    assert calls == [
        {
            "hermes_home": Path(tmp_path),
            "non_interactive": True,
            "start_gateway": True,
        }
    ]
    out = capsys.readouterr().out
    assert out.startswith(f"relic-gumi ready: {tmp_path / 'profiles' / HERMES_USER_PROFILE_NAME}\n")
    assert "Next: open Telegram and send /start to your bot" in out


def test_cli_no_start_reconciles_without_starting_gateway(monkeypatch, tmp_path, capsys):
    calls: list[dict[str, object]] = []

    class Installer:
        def install(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(profile_home=tmp_path / "profiles" / HERMES_USER_PROFILE_NAME)

    monkeypatch.setattr(cli, "Installer", Installer)

    assert cli.main(["install", "--hermes-home", str(tmp_path), "--no-start"]) == 0
    assert calls == [
        {
            "hermes_home": Path(tmp_path),
            "non_interactive": False,
            "start_gateway": False,
        }
    ]
    assert "reconciled offline" in capsys.readouterr().out


def _write_env(profile_home: Path, text: str, mode: int = 0o600) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    env = profile_home / ".env"
    env.write_text(text, encoding="utf-8")
    env.chmod(mode)


def _install_without_onboarding(monkeypatch, profile_home):
    events: list[str] = []

    class Installer:
        def install(self, **_kwargs):
            events.append("install")
            return SimpleNamespace(profile_home=profile_home)

    class UserOnboarding:
        def __init__(self, registry):
            events.append("onboard")

        def run(self):
            events.append("onboard")

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)
    monkeypatch.setattr(cli, "activate_runtime", lambda **_k: events.append("activate"))
    return events


def test_cli_install_hands_onboarding_off_to_telegram(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    _write_env(profile_home, "TELEGRAM_BOT_TOKEN=secret-token\nTELEGRAM_ALLOWED_USERS=123456789\n")
    seed_credentials({"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_USERS": "123456789"})
    events = _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    assert events == ["install"]
    assert capsys.readouterr().out == (
        f"relic-gumi ready: {profile_home}\n"
        "Next: open Telegram and send /start to your bot\n"
    )


def test_cli_install_lists_only_missing_telegram_names(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    seed_credentials({"TELEGRAM_BOT_TOKEN": "test-token"})
    _write_env(profile_home, "TELEGRAM_BOT_TOKEN=secret-token\n")
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Missing in the active Hermes secret scope: TELEGRAM_ALLOWED_USERS\n" in out
    assert "TELEGRAM_BOT_TOKEN" not in out
    assert "secret-token" not in out
    assert "Next: open Telegram and send /start to your bot" in out


def test_cli_install_missing_env_counts_as_both_missing(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    assert "Missing in the active Hermes secret scope: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS\n" in (
        capsys.readouterr().out
    )


def test_cli_install_unreadable_env_counts_as_both_missing(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    # The private reader rejects a symlinked .env, so its values must not be trusted.
    profile_home.mkdir(parents=True)
    real = tmp_path / "elsewhere.env"
    real.write_text("TELEGRAM_BOT_TOKEN=secret-token\nTELEGRAM_ALLOWED_USERS=123456789\n")
    (profile_home / ".env").symlink_to(real)
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Missing in the active Hermes secret scope: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS\n" in out
    assert "secret-token" not in out


def test_cli_non_interactive_install_prints_next_step(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    events = _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install", "--non-interactive"]) == 0
    assert events == ["install"]
    out = capsys.readouterr().out
    assert out.startswith(f"relic-gumi ready: {profile_home}\n")
    assert "Next: open Telegram and send /start to your bot" in out


def test_cli_no_start_install_does_not_print_next_step(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install", "--no-start"]) == 0
    assert capsys.readouterr().out == f"relic-gumi reconciled offline: {profile_home}\n"


def test_cli_configure_help_marks_it_advanced(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "advanced; normal onboarding happens on Telegram" in " ".join(
        capsys.readouterr().out.split()
    )


def test_cli_yes_approves_prerequisite_prompt(monkeypatch, capsys):
    prompts = []

    class Installer:
        def __init__(self, *, prompt):
            prompts.append(prompt("Install prerequisites?"))

        def install(self, **_kwargs):
            return SimpleNamespace(profile_home=Path("/profiles") / HERMES_USER_PROFILE_NAME)

    class UserOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            return None

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["install", "--yes"]) == 0
    assert prompts == [True]
    assert capsys.readouterr().out.startswith(f"relic-gumi ready: /profiles/{HERMES_USER_PROFILE_NAME}\n")


def test_cli_install_failure_never_starts_onboarding(monkeypatch, capsys):
    onboarding_started = []

    class Installer:
        def install(self, **_kwargs):
            raise OSError("profile unavailable")

    class UserOnboarding:
        def __init__(self, registry):
            onboarding_started.append(True)

        def run(self):
            onboarding_started.append(True)

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["install"]) == 1
    assert onboarding_started == []
    assert capsys.readouterr().out == ""


def test_cli_configure_reaches_onboarding(monkeypatch, tmp_path):
    events: list[str] = []

    class UserOnboarding:
        def __init__(self, registry):
            assert registry.hermes_profiles_home == tmp_path / "profiles"

        def run(self):
            events.append("configure")
            return None

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 0
    assert events == ["configure"]


def test_cli_configure_cancelled_returns_zero(monkeypatch, tmp_path, capsys):
    class UserOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            return None

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


def test_cli_configure_onboarding_failure_is_nonzero(monkeypatch, tmp_path, capsys):
    class UserOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            raise ValueError("onboarding state is corrupt")

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    assert "onboarding state is corrupt" in capsys.readouterr().err


def test_cli_configure_non_interactive_does_not_prompt(monkeypatch, tmp_path):
    calls = []

    class UserOnboarding:
        def __init__(self, registry):
            calls.append(registry)

        def run(self):
            calls.append("run")

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["configure", "--hermes-home", str(tmp_path), "--non-interactive"]) == 1
    assert calls == []


@pytest.mark.parametrize(
    "error",
    [OSError("profile unavailable"), yaml.YAMLError("invalid YAML"), ValueError("invalid state")],
)
def test_cli_reports_expected_installer_errors(monkeypatch, capsys, error):
    class Installer:
        def install(self, **_kwargs):
            raise error

    monkeypatch.setattr(cli, "Installer", Installer)

    assert cli.main(["install"]) == 1
    assert str(error) in capsys.readouterr().err


def test_cli_configure_runs_singleton_user_onboarding(monkeypatch, tmp_path, capsys):
    calls: list[Path] = []

    class ProfileRegistry:
        def __init__(self, *, hermes_profiles_home):
            calls.append(hermes_profiles_home)

    class UserOnboarding:
        def __init__(self, registry):
            assert isinstance(registry, ProfileRegistry)

        def run(self):
            return None

    monkeypatch.setattr(cli, "ProfileRegistry", ProfileRegistry)
    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 0
    assert calls == [tmp_path / "profiles"]
    assert capsys.readouterr().out == ""


def test_cli_configure_activates_runtime_after_onboarding(monkeypatch, tmp_path, capsys):
    events: list[str] = []

    class UserOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            events.append("onboard")
            return SimpleNamespace(configuration_path=tmp_path / "config.json")

    def activate_runtime(*, hermes_base_home):
        events.append("activate")
        return SimpleNamespace(recurring_active=True, first_message_scheduled=False)
    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)
    monkeypatch.setattr(cli, "activate_runtime", activate_runtime)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 0
    assert events == ["onboard", "activate"]
    assert capsys.readouterr().out == (
        "relic-gumi delivery: recurring check-ins scheduled; first message not scheduled.\n"
    )


def test_cli_configure_reports_activation_failure(monkeypatch, tmp_path, capsys):
    class UserOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            return SimpleNamespace(configuration_path=tmp_path / "config.json")

    def activate_runtime(*, hermes_base_home):
        raise cli.RuntimeActivationError("cron activation failed")

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)
    monkeypatch.setattr(cli, "activate_runtime", activate_runtime)

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cron activation failed" in captured.err


def test_cli_configure_final_summary_uses_user_language_and_recovery_command(
    monkeypatch, tmp_path, capsys
):
    base_home = tmp_path / "hermes"
    profile_home = base_home / "profiles" / HERMES_USER_PROFILE_NAME
    base_home.mkdir()

    class UserOnboarding:
        def __init__(self, registry):
            assert registry.hermes_profiles_home == profile_home.parent

        def run(self):
            return SimpleNamespace(
                media={
                    "images": {"selected": True, "enabled": True},
                    "audio": {"selected": True, "enabled": False},
                    "music": {"selected": True, "enabled": True},
                },
                delivery_capability_enabled=True,
                delivery_execution_armed=False,
            )

    monkeypatch.setattr(cli, "UserOnboarding", UserOnboarding)
    monkeypatch.setattr(
        cli,
        "activate_runtime",
        lambda **_kwargs: SimpleNamespace(recurring_active=False, first_message_scheduled=False),
    )

    assert cli.main(["configure", "--hermes-home", str(base_home)]) == 0
    output = capsys.readouterr().out
    assert output == (
        "relic-gumi summary:\n"
        "dependencies: ready\n"
        "plugin/hooks: ready\n"
        "gateway: ready\n"
        "Telegram: needs setup\n"
        "media: images on; audio off; music on\n"
        "cron: inactive\n"
        "first message: waiting\n"
        f"recovery: relic-gumi configure --hermes-home {base_home}\n"
        f"relic-gumi ready: {profile_home}\n"
    )
    lowered = output.lower()
    assert all(
        word not in lowered
        for word in ("subject", "experiment", "researcher", "consent", "emergency", "risk")
    )
