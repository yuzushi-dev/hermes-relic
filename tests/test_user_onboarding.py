from __future__ import annotations

import json
import stat
from io import StringIO
from pathlib import Path

import pytest

from relic.profile import user_onboarding
from relic.profile.registry import LegacyProfileMigrationRequired, ProfileRegistry
from relic.profile.user_onboarding import GUMI_CLASSIC, UserOnboarding


VALID_TELEGRAM_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
VALID_GEMINI_KEY = "AIza" + "a" * 35


def _registry(tmp_path: Path) -> ProfileRegistry:
    return ProfileRegistry(
        relic_home=tmp_path / "relic",
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )


def _input(*lines: str) -> StringIO:
    return StringIO("\n".join(lines) + "\n")


DEFAULT_BATTERY_RESPONSES = ("",) * 48


def _onboarding_input(mode: str, *after_baseline: str) -> StringIO:
    return _input(mode, "c", *DEFAULT_BATTERY_RESPONSES, *after_baseline)


def _artifacts(registry: ProfileRegistry) -> dict[str, dict[str, object]]:
    artifacts = registry.load_user_artifacts()
    assert artifacts is not None
    return artifacts


def _seed_canonical_artifacts(registry: ProfileRegistry, state: dict[str, object]) -> None:
    user = registry.initialize_user()
    credentials = dict(state.get("credentials", {}))
    private_env = {
        {
            "gemini_api_key": "GEMINI_API_KEY",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_target": "TELEGRAM_HOME_CHANNEL",
        }.get(name, name.upper()): value
        for name, value in credentials.items()
        if isinstance(name, str) and isinstance(value, str)
    }
    registry.materialize_user_artifacts(
        user,
        baseline={},
        gumi_profile={
            "flavor": {"id": GUMI_CLASSIC.identifier, "version": GUMI_CLASSIC.version},
            "setup_mode": "default",
            "preferences": state.get("preferences", GUMI_CLASSIC.preferences()),
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": False,
            "delivery_execution_armed": False,
            "first_message": "wait",
            "credential_references": {},
        },
        checkin_state={
            "recurring": {"status": "off"},
            "first_message": {"status": "waiting"},
        },
        private_env=private_env,
    )


def test_default_prompt_order_and_artifacts_are_user_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(user_onboarding, "_locale_language", lambda: "it")
    registry = _registry(tmp_path)
    output = StringIO()

    result = UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "", "w"),
        io_out=output,
        environ={},
    ).run()

    assert result is not None
    text = output.getvalue()
    prompts = [
        "Choose your setup: [D]efault (recommended) / [C]ustom / [Q]uit",
        "Choose Gumi [C]lassic",
        "Gumi Classic baseline questions",
        "Enable images? [y/N]",
        "Enable audio? [y/N]",
        "Enable music? [y/N]",
        "Telegram bot token (leave blank to keep delivery off)",
        "First message [G]umi sends first / [W]ait for me",
    ]
    assert [text.index(prompt) for prompt in prompts] == sorted(text.index(prompt) for prompt in prompts)
    assert "Gemini API key" not in text
    assert "Customize message style" not in text
    assert text.count("Your answer [") == 48
    forbidden_terms = (
        "what should gumi call you?",
        "which timezone should gumi use?",
        "which language should gumi use?",
        "self-report",
        "subject",
        "experiment",
        "researcher",
        "consent",
        "emergency",
        "risk",
    )
    for term in forbidden_terms:
        assert term not in text.lower()

    artifacts = _artifacts(registry)
    profile = artifacts["user_profile"]
    baseline = artifacts["baseline_response"]["baseline"]
    gumi = artifacts["gumi_profile"]
    policy = artifacts["runtime_policy"]
    assert profile["runtime_scope_id"] == result.user.runtime_scope_id
    assert gumi["flavor"] == {"id": "gumi-classic", "version": 1}
    assert policy["delivery_capability_enabled"] is False
    assert policy["delivery_execution_armed"] is False
    assert policy["first_message"] == "wait"
    assert baseline["display_name"] is None
    assert baseline["language"] == "it"
    assert baseline["timezone"] is None
    assert baseline["battery"]["user_visible_item_count"] == 48
    assert baseline["battery"]["responses"]["SAFE_010"] == 1
    assert stat.S_IMODE(registry.user_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(registry.runtime_policy_path.stat().st_mode) == 0o600


def test_gemini_is_prompted_only_for_selected_media_and_missing_key_disables_it(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "y", "n", "n", "", "", "", "w"),
        io_out=output,
        environ={},
    ).run()

    text = output.getvalue()
    gemini_prompt = "Gemini API key (leave blank to keep selected media off)"
    assert text.count(gemini_prompt) == 1
    prompts = [
        "Enable music? [y/N]",
        gemini_prompt,
        "Telegram bot token (leave blank to keep delivery off)",
    ]
    assert [text.index(prompt) for prompt in prompts] == sorted(text.index(prompt) for prompt in prompts)
    gumi = _artifacts(registry)["gumi_profile"]
    assert gumi["media"]["images"] == {
        "selected": True,
        "enabled": False,
        "remediation": "Add a Gemini API key with relic-gumi configure.",
    }
    assert gumi["media"]["audio"]["enabled"] is False
    assert gumi["media"]["music"]["enabled"] is False


def test_telegram_uses_valid_canonical_values_then_arms_recurring_delivery(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "y", "y", "w"),
        io_out=output,
        environ={
            "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": "123456789",
        },
    ).run()

    text = output.getvalue()
    assert "Telegram bot token (leave blank to keep delivery off)" not in text
    assert "Telegram target (numbers only; leave blank to keep delivery off)" not in text
    assert "Arm recurring Telegram messages? They can arrive without opening Gumi. [Y/n]" in text
    assert VALID_TELEGRAM_TOKEN not in text
    policy = _artifacts(registry)["runtime_policy"]
    assert policy["delivery_capability_enabled"] is True
    assert policy["delivery_execution_armed"] is True
    env = registry.hermes_user_env_path.read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={VALID_TELEGRAM_TOKEN}" in env
    assert "TELEGRAM_HOME_CHANNEL=123456789" in env
    assert stat.S_IMODE(registry.hermes_user_env_path.stat().st_mode) == 0o600

def test_default_setup_arms_delivery_and_requests_first_message(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    result = UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "", "", ""),
        io_out=StringIO(),
        environ={
            "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": "123456789",
        },
    ).run()

    assert result is not None
    policy = _artifacts(registry)["runtime_policy"]
    checkin = _artifacts(registry)["checkin_state"]
    assert policy["delivery_execution_armed"] is True
    assert policy["first_message"] == "gumi_sends_first"
    assert checkin["recurring"]["status"] == "armed"
    assert checkin["first_message"]["status"] == "requested"


def test_invalid_telegram_values_are_requested_then_leave_delivery_unarmed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "", "w"),
        io_out=output,
        environ={"TELEGRAM_BOT_TOKEN": "not-a-token", "TELEGRAM_HOME_CHANNEL": "abc"},
    ).run()

    text = output.getvalue()
    assert "Telegram bot token (leave blank to keep delivery off)" in text
    assert "Telegram target (numbers only; leave blank to keep delivery off)" not in text
    assert "Telegram delivery remains off." in text
    policy = _artifacts(registry)["runtime_policy"]
    assert policy["delivery_capability_enabled"] is False
    assert policy["delivery_execution_armed"] is False


def test_telegram_capability_is_not_armed_after_an_explicit_no(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "y", "n", "w"),
        io_out=StringIO(),
        environ={
            "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": "123456789",
        },
    ).run()

    policy = _artifacts(registry)["runtime_policy"]
    assert policy["delivery_capability_enabled"] is True
    assert policy["delivery_execution_armed"] is False


class _InterruptingInput:
    def __init__(self, *events: str | BaseException) -> None:
        self._events = iter(events)

    def readline(self) -> str:
        event = next(self._events)
        if isinstance(event, BaseException):
            raise event
        return event


def test_two_consecutive_interrupts_discard_custom_state_and_return_to_mode_choice(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.initialize_user()
    output = StringIO()

    result = UserOnboarding(
        registry,
        io_in=_InterruptingInput("c\n", "c\n", KeyboardInterrupt(), KeyboardInterrupt(), "q\n"),
        io_out=output,
        environ={},
    ).run()

    assert result is None
    assert registry.user_profile_path.exists()
    assert not registry.gumi_profile_path.exists()
    text = output.getvalue()
    assert "Press Ctrl+C again to discard your current choices." in text
    assert "Your current choices were discarded. Returning to setup selection." in text


def test_custom_exposes_advanced_preferences_without_changing_classic_defaults(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()
    classic_preferences = GUMI_CLASSIC.preferences()

    UserOnboarding(
        registry,
        io_in=_onboarding_input(
            "c", "y", "detailed", "frequent", "n", "n", "n", "n", "n", "", "", "w"
        ),
        io_out=output,
        environ={},
    ).run()

    gumi = _artifacts(registry)["gumi_profile"]
    assert "Customize message style? [y/N]" in output.getvalue()
    assert gumi["preferences"]["interaction"]["message_length"] == "detailed"
    assert gumi["preferences"]["interaction"]["emoji_use"] == "frequent"
    assert GUMI_CLASSIC.preferences() == classic_preferences


def test_existing_gemini_key_enables_selected_media_without_prompting(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "y", "n", "n", "", "", "w"),
        io_out=output,
        environ={"GEMINI_API_KEY": VALID_GEMINI_KEY},
    ).run()

    gumi = _artifacts(registry)["gumi_profile"]
    assert "Gemini API key" not in output.getvalue()
    assert gumi["media"]["images"] == {"selected": True, "enabled": True}
    assert VALID_GEMINI_KEY in registry.hermes_user_env_path.read_text(encoding="utf-8")


def test_telegram_only_uses_the_canonical_user_profile_env(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    other_env = registry.hermes_profiles_home / "other" / ".env"
    other_env.parent.mkdir(parents=True)
    other_env.write_text(
        f"TELEGRAM_BOT_TOKEN={VALID_TELEGRAM_TOKEN}\nTELEGRAM_HOME_CHANNEL=123456789\n",
        encoding="utf-8",
    )
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "", "", "w"),
        io_out=output,
        environ={},
    ).run()

    policy = _artifacts(registry)["runtime_policy"]
    assert "Telegram bot token (leave blank to keep delivery off)" in output.getvalue()
    assert policy["delivery_capability_enabled"] is False


def test_existing_credentials_and_preferences_survive_reconfiguration(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    saved_preferences = {
        "interaction": {"message_length": "detailed", "emoji_use": "none"},
        "relational": {"tone": "direct"},
        "safety": {"romantic_language": True},
        "proactivity": {"maximum_frequency": "1/day", "quiet_hours": "20:00-06:00"},
    }
    saved_credentials = {
        "gemini_api_key": VALID_GEMINI_KEY,
        "telegram_bot_token": VALID_TELEGRAM_TOKEN,
        "telegram_target": "123456789",
        "unrelated_credential": "keep-me",
    }
    _seed_canonical_artifacts(
        registry,
        {"preferences": saved_preferences, "credentials": saved_credentials},
    )

    output = StringIO()
    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "n", "w"),
        io_out=output,
        environ={},
    ).run()

    gumi = _artifacts(registry)["gumi_profile"]
    env = registry.hermes_user_env_path.read_text(encoding="utf-8")
    assert f"GEMINI_API_KEY={VALID_GEMINI_KEY}" in env
    assert f"TELEGRAM_BOT_TOKEN={VALID_TELEGRAM_TOKEN}" in env
    assert "TELEGRAM_HOME_CHANNEL=123456789" in env
    assert "UNRELATED_CREDENTIAL=keep-me" in env
    assert gumi["preferences"]["interaction"]["message_length"] == "detailed"
    assert gumi["preferences"]["interaction"]["emoji_use"] == "none"
    assert gumi["preferences"]["relational"]["tone"] == "direct"
    assert gumi["preferences"]["safety"]["romantic_language"] is True
    assert gumi["preferences"]["proactivity"]["maximum_frequency"] == "1/day"
    assert gumi["preferences"]["proactivity"]["quiet_hours"] == "20:00-06:00"
    assert VALID_TELEGRAM_TOKEN not in output.getvalue()
    assert len(registry.onboarding_provenance_path.read_text(encoding="utf-8").splitlines()) == 2


def test_telegram_does_not_mix_values_between_canonical_sources(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _seed_canonical_artifacts(registry, {"credentials": {"telegram_target": "123456789"}})
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "n", "n", "n", "", "w"),
        io_out=output,
        environ={"TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN},
    ).run()

    text = output.getvalue()
    assert "Telegram bot token (leave blank to keep delivery off)" in text
    assert "Telegram target (numbers only; leave blank to keep delivery off)" not in text
    assert "TELEGRAM_HOME_CHANNEL=123456789" in registry.hermes_user_env_path.read_text(
        encoding="utf-8"
    )
    assert _artifacts(registry)["runtime_policy"]["delivery_capability_enabled"] is False


def test_read_env_accepts_exported_matching_quotes(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        f'export GEMINI_API_KEY="{VALID_GEMINI_KEY}"\n'
        f"export TELEGRAM_BOT_TOKEN='{VALID_TELEGRAM_TOKEN}'\n"
        "export TELEGRAM_HOME_CHANNEL='123456789'\n",
        encoding="utf-8",
    )

    assert UserOnboarding._read_env(path) == {
        "GEMINI_API_KEY": VALID_GEMINI_KEY,
        "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
        "TELEGRAM_HOME_CHANNEL": "123456789",
    }


def test_legacy_profile_is_rejected_before_any_prompt(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    legacy = registry.subjects_dir / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "subject_profile.json").write_text("{}", encoding="utf-8")
    output = StringIO()

    with pytest.raises(LegacyProfileMigrationRequired, match="explicit migration"):
        UserOnboarding(registry, io_in=StringIO(), io_out=output, environ={}).run()

    assert output.getvalue() == ""
    assert registry.load_user() is None


def test_eof_cancels_without_saving_onboarding(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    result = UserOnboarding(registry, io_in=StringIO(), io_out=output, environ={}).run()

    assert result is None
    assert not registry.gumi_profile_path.exists()
    assert "Setup cancelled. Nothing was saved." in output.getvalue()


def test_two_consecutive_interrupts_at_mode_choice_cancel_setup(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    result = UserOnboarding(
        registry,
        io_in=_InterruptingInput(KeyboardInterrupt(), KeyboardInterrupt()),
        io_out=output,
        environ={},
    ).run()

    assert result is None
    assert not registry.gumi_profile_path.exists()
    text = output.getvalue()
    assert "Press Ctrl+C again to discard your current choices." in text
    assert "Setup cancelled. Nothing was saved." in text
    assert "Returning to setup selection." not in text


def test_custom_quiet_hours_reprompts_until_valid(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    output = StringIO()

    UserOnboarding(
        registry,
        io_in=_onboarding_input(
            "c",
            "n",
            "n",
            "n",
            "n",
            "n",
            "n",
            "y",
            "n",
            "y",
            "1/day",
            "not-a-range",
            "21:30-07:45",
            "w",
        ),
        io_out=output,
        environ={
            "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": "123456789",
        },
    ).run()

    text = output.getvalue()
    assert text.count("Quiet hours [22:00-08:00]") == 2
    assert "Enter quiet hours as HH:MM-HH:MM." in text
    gumi = _artifacts(registry)["gumi_profile"]
    assert gumi["preferences"]["proactivity"]["quiet_hours"] == "21:30-07:45"

def test_onboarding_materializes_only_private_canonical_artifacts(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    result = UserOnboarding(
        registry,
        io_in=_onboarding_input("d", "y", "n", "n", "y", "y", "g"),
        io_out=StringIO(),
        environ={
            "GEMINI_API_KEY": VALID_GEMINI_KEY,
            "TELEGRAM_BOT_TOKEN": VALID_TELEGRAM_TOKEN,
            "TELEGRAM_HOME_CHANNEL": "123456789",
        },
    ).run()

    assert result is not None
    assert {entry.name for entry in registry.user_home.iterdir()} == {
        "user_profile.json",
        "baseline_response.json",
        "gumi_profile.json",
        "runtime_policy.json",
        "checkin_state.json",
        "provenance",
    }
    artifact_names = (
        "user_profile.json",
        "baseline_response.json",
        "gumi_profile.json",
        "runtime_policy.json",
        "checkin_state.json",
    )
    forbidden = {"subject", "experiment", "researcher", "consent", "emergency", "risk"}
    for name in artifact_names:
        path = registry.user_home / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["runtime_scope_id"] == result.user.runtime_scope_id
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not (set(_artifact_field_names(payload)) & forbidden)
        assert VALID_GEMINI_KEY not in path.read_text(encoding="utf-8")
        assert VALID_TELEGRAM_TOKEN not in path.read_text(encoding="utf-8")

    provenance = registry.user_home / "provenance" / "onboarding.jsonl"
    assert provenance.is_file()
    assert len(provenance.read_text(encoding="utf-8").splitlines()) == 1
    assert stat.S_IMODE(provenance.stat().st_mode) == 0o600
    assert stat.S_IMODE(provenance.parent.stat().st_mode) == 0o700
    private_env = registry.hermes_user_home / ".env"
    assert VALID_GEMINI_KEY in private_env.read_text(encoding="utf-8")
    assert VALID_TELEGRAM_TOKEN in private_env.read_text(encoding="utf-8")


def _artifact_field_names(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_artifact_field_names(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_artifact_field_names(item) for item in value)) if value else set()
    return set()
