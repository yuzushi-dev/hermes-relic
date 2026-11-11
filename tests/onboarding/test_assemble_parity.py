"""Parity between the scripted CLI onboarding and the Telegram answer assembly."""

from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")

import json
import os
import stat
from io import StringIO
from pathlib import Path


from relic.hermes_plugin.context_injection import _build_user_runtime_guidance
from relic.onboarding.assemble import assemble, finalize
from relic.onboarding.questions import build_questions
from relic.profile import user_onboarding
from relic.profile._bootstrap_steps import item_battery as ib
from relic.profile.registry import ProfileRegistry
from relic.profile.user_onboarding import UserOnboarding

TOKEN = "111111111:" + "A" * 35  # fake, built at runtime
CHAT_ID = "123456789"


def _registry(tmp_path: Path, name: str) -> ProfileRegistry:
    return ProfileRegistry(
        relic_home=tmp_path / name / "relic",
        hermes_profiles_home=tmp_path / name / "hermes" / "profiles",
    )


# (direct item, reverse-scored item) of each TIPI trait: two single questions.
_TIPI_PAIRS = (
    ("TIPI_001", "TIPI_006"),
    ("TIPI_007", "TIPI_002"),
    ("TIPI_003", "TIPI_008"),
    ("TIPI_009", "TIPI_004"),
    ("TIPI_005", "TIPI_010"),
)


def _battery_values() -> dict[str, int]:
    """One non-default value per visible item, shared by the CLI script and Telegram.

    The two TIPI items of each trait are made consistent (inverse = 8 - r),
    because the Telegram questions can only express that.
    """
    values: dict[str, int] = {}
    for index, item in enumerate(ib.USER_BASELINE_ITEMS):
        low, high, _ = ib._scale_bounds(item["response_scale"])
        values[item["item_id"]] = low + (index * 5 + 2) % (high - low + 1)
    # The 11 items without a chat reader are not asked on Telegram and get their
    # conservative defaults there: give them the same value on the CLI side.
    asked = {q.item_id for q in build_questions() if q.item_id}
    for item in ib.USER_BASELINE_ITEMS:
        if item["item_id"] not in asked:
            values[item["item_id"]] = ib._conservative_default(item)
    for direct, inverse in _TIPI_PAIRS:
        values[inverse] = 8 - values[direct]
    assert len(values) == 48
    return values


def _telegram_answers(values: dict[str, int]) -> dict[str, str]:
    """The Telegram answers (b01..b37) that stand for the given per-item values."""
    answers: dict[str, str] = {}
    for question in build_questions():
        if question.item_id:
            answers[question.id] = str(values[question.item_id])
    assert len(answers) == 37
    return answers


def _quick_answers(**overrides: str) -> dict[str, str]:
    answers = {
        "name": "__skip__",
        "tz": "Europe/Rome",
        "mode": "default",
        **_telegram_answers(_battery_values()),
        "img": "no",
        "aud": "no",
        "mus": "no",
        "armed": "yes",
        "c_act": "no",
        "c_pas": "no",
    }
    answers.update(overrides)
    return answers


def _run_cli(registry: ProfileRegistry, *, armed: bool = True) -> None:
    # mode, flavor, the 48 battery answers (same values as the Telegram side),
    # 3 media "no", then Telegram.
    # armed: token + numeric target + accept the default "arm" + keep the delivery
    # defaults, first message: Gumi sends first. Not armed: blank token (delivery
    # stays off), first message: wait for the user.
    seed_credentials({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_HOME_CHANNEL": CHAT_ID})
    telegram = ["y", "y", "g"] if armed else ["y", "n", "w"]
    values = _battery_values()
    battery = [str(values[item["item_id"]]) for item in ib.USER_BASELINE_ITEMS]
    script = ["d", "c", *battery, "", "", "", *telegram]
    result = UserOnboarding(
        registry,
        io_in=StringIO("\n".join(script) + "\n"),
        io_out=StringIO(),
        environ={},
    ).run()
    assert result is not None


def _strip_scope(artifact: dict) -> dict:
    return {key: value for key, value in artifact.items() if key != "runtime_scope_id"}


def test_default_assembly_matches_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(user_onboarding, "_locale_language", lambda: "it")
    cli = _registry(tmp_path, "cli")
    _run_cli(cli)

    tg = _registry(tmp_path, "tg")
    user = tg.initialize_user()
    assembled = assemble(
        _quick_answers(), telegram_token=TOKEN, chat_id=CHAT_ID, language="it"
    )
    finalize(tg, user, assembled)

    cli_art = cli.load_user_artifacts()
    tg_art = tg.load_user_artifacts()
    assert cli_art is not None and tg_art is not None

    # Differences by construction, removed before comparing:
    #  - runtime_scope_id: random per registry.
    #  - credential_references / delivery_capability_enabled: the scripted CLI ran with
    #    Telegram off, Telegram onboarding always has the channel (installer configured it).
    #  - baseline.timezone: the CLI never asks for it (None), Telegram asks for tz.
    cli_baseline = json.loads(json.dumps(cli_art["baseline_response"]))
    tg_baseline = json.loads(json.dumps(tg_art["baseline_response"]))
    assert cli_baseline["baseline"].pop("timezone") is None
    assert tg_baseline["baseline"].pop("timezone") == "Europe/Rome"
    # Telegram records which wording it used; the CLI does not.
    assert "battery_presentation" not in cli_baseline["baseline"]
    assert tg_baseline["baseline"].pop("battery_presentation") == "telegram-v2"
    # Same values in, same responses and scores out (the wording differs, the data does not).
    cli_battery = cli_baseline["baseline"]["battery"]
    tg_battery = tg_baseline["baseline"]["battery"]
    assert tg_battery["responses"] == cli_battery["responses"]
    assert tg_battery["scores"] == cli_battery["scores"]
    assert {k: tg_battery["responses"][k] for k in _battery_values()} == _battery_values()
    assert _strip_scope(cli_baseline) == _strip_scope(tg_baseline)

    assert _strip_scope(cli_art["gumi_profile"]) == _strip_scope(tg_art["gumi_profile"])
    assert _strip_scope(cli_art["checkin_state"]) == _strip_scope(tg_art["checkin_state"])

    def policy(artifact: dict) -> dict:
        data = _strip_scope(artifact["runtime_policy"])
        data.pop("credential_references")
        data.pop("delivery_capability_enabled")
        return data

    assert policy(cli_art) == policy(tg_art)
    assert tg_art["runtime_policy"]["delivery_capability_enabled"] is True


def test_custom_choices_reach_config_and_runtime_guidance(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "tg")
    user = registry.initialize_user()
    answers = _quick_answers(
        mode="custom",
        msglen="detailed",
        emoji="none",
        tone="direct",
        cont="always_off",
        safe1="yes",
        safe2="yes",
        armed="yes",
        freq="3/week",
        quiet="23:00-07:00",
        name="Ada",
        img="yes",
    )
    assembled = assemble(answers, telegram_token=TOKEN, chat_id=CHAT_ID, gemini_key="")
    finalize(registry, user, assembled)

    config = registry.load_user_configuration()
    assert config is not None
    prefs = config["preferences"]
    assert prefs["interaction"]["message_length"] == "detailed"
    assert prefs["interaction"]["emoji_use"] == "none"
    assert prefs["relational"]["tone"] == "direct"
    assert prefs["relational"]["continuity"] == "always_off"
    assert prefs["safety"]["romantic_language"] is True
    assert prefs["safety"]["sexual_language"] is True
    # exclusivity_language and guilt_after_no_reply are fixed always-off rules,
    # no longer asked in chat: they keep their (false) defaults.
    assert prefs["safety"]["exclusivity_language"] is False
    assert prefs["safety"]["guilt_after_no_reply"] is False
    assert prefs["proactivity"]["maximum_frequency"] == "3/week"
    assert prefs["proactivity"]["quiet_hours"] == "23:00-07:00"
    assert config["baseline"]["display_name"] == "Ada"
    assert config["delivery_execution_armed"] is True
    assert config["checkin_state"]["recurring"]["status"] == "armed"
    assert config["checkin_state"]["first_message"]["status"] == "requested"
    # Image selected but no Gemini key: selected stays, enabled is off with remediation.
    assert config["media"]["images"]["selected"] is True
    assert config["media"]["images"]["enabled"] is False
    assert "remediation" in config["media"]["images"]

    guidance = _build_user_runtime_guidance(config)
    for expected in ("detailed", "none", "direct", "always_off"):
        assert expected in guidance


def test_finalize_writes_consents_and_preserves_other_keys(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "tg")
    user = registry.initialize_user()
    policy_path = registry.user_home / "checkin" / "delivery_policy.json"
    policy_path.parent.mkdir(mode=0o700, parents=True)
    policy_path.write_text(
        json.dumps({"PRO_IMAGE": 1, "consent_for_active_elicitation": False}),
        encoding="utf-8",
    )
    answers = _quick_answers(c_act="yes", c_pas="yes")
    finalize(registry, user, assemble(answers, telegram_token=TOKEN, chat_id=CHAT_ID))

    data = json.loads(policy_path.read_text(encoding="utf-8"))
    assert data["consent_for_active_elicitation"] is True
    assert data["consent_for_passive_extraction"] is True
    assert data["PRO_IMAGE"] == 1
    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(policy_path.parent.stat().st_mode) == 0o700
    assert not list(policy_path.parent.glob(".*tmp*"))


def test_finalize_creates_consent_file_when_missing_with_declined_consents(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, "tg")
    user = registry.initialize_user()
    finalize(registry, user, assemble(_quick_answers(c_act="yes"), telegram_token=TOKEN, chat_id=CHAT_ID))
    policy_path = registry.user_home / "checkin" / "delivery_policy.json"
    assert json.loads(policy_path.read_text(encoding="utf-8")) == {
        "consent_for_active_elicitation": True,
        "consent_for_passive_extraction": False,
    }
    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(policy_path.parent.stat().st_mode) == 0o700


def test_consents_stay_false_without_a_tap_and_true_with_recommended_yes() -> None:
    answers = _quick_answers()
    del answers["c_act"], answers["c_pas"]
    untouched = assemble(answers, telegram_token=TOKEN, chat_id=CHAT_ID)
    assert untouched.consents == {
        "consent_for_active_elicitation": False,
        "consent_for_passive_extraction": False,
    }
    # "Yes (recommended)" is a button to tap: its value is "yes"
    label_to_value = {o.label: o.value for q in build_questions() if q.id == "c_act" for o in q.options}
    tapped = assemble(
        _quick_answers(c_act=label_to_value["Yes (recommended)"], c_pas="yes"),
        telegram_token=TOKEN,
        chat_id=CHAT_ID,
    )
    assert tapped.consents == {
        "consent_for_active_elicitation": True,
        "consent_for_passive_extraction": True,
    }


def test_not_armed_assembly_waits_and_matches_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(user_onboarding, "_locale_language", lambda: "it")
    cli = _registry(tmp_path, "cli")
    _run_cli(cli, armed=False)

    tg = _registry(tmp_path, "tg")
    user = tg.initialize_user()
    assembled = assemble(
        _quick_answers(armed="no"), telegram_token=TOKEN, chat_id=CHAT_ID, language="it"
    )
    finalize(tg, user, assembled)

    cli_art = cli.load_user_artifacts()
    tg_art = tg.load_user_artifacts()
    assert cli_art is not None and tg_art is not None
    assert tg_art["runtime_policy"]["first_message"] == "wait"
    assert tg_art["checkin_state"]["first_message"]["status"] == "waiting"
    assert tg_art["checkin_state"]["recurring"]["status"] == "off"
    assert _strip_scope(cli_art["checkin_state"]) == _strip_scope(tg_art["checkin_state"])
    assert cli_art["runtime_policy"]["first_message"] == tg_art["runtime_policy"]["first_message"]


@pytest.mark.parametrize(
    "override",
    [
        {"quiet": "bad"},
        {"quiet": "25:00-07:00"},
        {"freq": "5/day"},
        {"msglen": "huge"},
        {"b01": "9"},
    ],
)
def test_invalid_answers_raise_value_error(override: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        assemble(_quick_answers(**override), telegram_token=TOKEN, chat_id=CHAT_ID)


def test_quiet_skip_and_missing_use_default() -> None:
    skipped = assemble(
        _quick_answers(mode="custom", armed="yes", quiet="__skip__"),
        telegram_token=TOKEN,
        chat_id=CHAT_ID,
    )
    assert skipped.gumi_profile["preferences"]["proactivity"]["quiet_hours"] == "22:00-08:00"
    missing = assemble(_quick_answers(), telegram_token=TOKEN, chat_id=CHAT_ID)
    assert missing.gumi_profile["preferences"]["proactivity"]["quiet_hours"] == "22:00-08:00"
    assert missing.gumi_profile["preferences"]["proactivity"]["maximum_frequency"] == "2/day"


def test_secrets_only_in_private_env(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "tg")
    user = registry.initialize_user()
    gemini = "AIza" + "b" * 35
    assembled = assemble(
        _quick_answers(img="yes"), telegram_token=TOKEN, chat_id=CHAT_ID, gemini_key=gemini
    )
    assert assembled.private_env == {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_HOME_CHANNEL": CHAT_ID,
        "GEMINI_API_KEY": gemini,
    }
    finalize(registry, user, assembled)

    for path in registry.user_home.rglob("*.json*"):
        text = path.read_text(encoding="utf-8")
        assert TOKEN not in text, path
        assert gemini not in text, path
    env_path = registry.hermes_user_env_path
    assert not env_path.exists()
