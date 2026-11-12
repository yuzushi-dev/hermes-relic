from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials
pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

from pathlib import Path
from types import SimpleNamespace

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
    out = capsys.readouterr().out
    assert out.startswith(f"relic-gumi ready: {profile_home}\n")
    assert "Next: open Telegram and send /start to your bot" in out
    assert "Credentials not checked" in out and "/gumi-setup" in out


def test_cli_install_lists_only_missing_telegram_names(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    seed_credentials({"TELEGRAM_BOT_TOKEN": "test-token"})
    _write_env(profile_home, "TELEGRAM_BOT_TOKEN=secret-token\n")
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Credentials not checked" in out
    assert "Missing in" not in out
    assert "secret-token" not in out
    assert "Next: open Telegram and send /start to your bot" in out


def test_cli_install_missing_env_counts_as_both_missing(monkeypatch, tmp_path, capsys):
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    _install_without_onboarding(monkeypatch, profile_home)

    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Credentials not checked" in out
    assert "Missing in" not in out


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
    assert "Credentials not checked" in out
    assert "Missing in" not in out
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
    assert "use /gumi-setup inside the target Hermes profile" in " ".join(
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


def test_cli_configure_defers_onboarding(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "UserOnboarding", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


def test_cli_configure_does_not_prompt(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "UserOnboarding", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


def test_cli_configure_does_not_read_onboarding_state(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "UserOnboarding", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


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


def test_cli_configure_does_not_create_singleton_user(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "ProfileRegistry", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


def test_cli_configure_does_not_activate_runtime(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "activate_runtime", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


def test_cli_configure_does_not_enter_activation(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("standalone CLI must defer to target-profile Hermes")
    monkeypatch.setattr(cli, "activate_runtime", forbidden)
    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "Credentials not checked" in output
    assert "/gumi-setup" in output
    assert not (tmp_path / "profiles").exists()


def test_cli_configure_final_summary_uses_user_language_and_recovery_command(
    monkeypatch, tmp_path, capsys
):
    base_home = tmp_path / "hermes"
    profile_home = base_home / "profiles" / HERMES_USER_PROFILE_NAME
    base_home.mkdir()

    class UserOnboarding:
        def __init__(self, registry):
            pass

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

    cli._print_final_summary(profile_home, UserOnboarding(None).run(),
                             SimpleNamespace(recurring_active=False, first_message_scheduled=False))
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
