"""Turn Telegram onboarding answers into the canonical User configuration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from relic.onboarding.battery_text import BATTERY_PRESENTATION
from relic.onboarding.engine import SKIP
from relic.onboarding.questions import build_questions
from relic.profile._bootstrap_steps.item_battery import battery_from_responses
from relic.profile.registry import ProfileRegistry, UserProfile
from relic.profile.user_onboarding import (
    _QUIET_HOURS,
    _REMEDIATION,
    GUMI_CLASSIC,
    UserOnboarding,
    _artifact_safe,
)

_CONSENT_FILE = "delivery_policy.json"

# answer id -> (preference group, preference key)
_CHOICE_PREFERENCES = {
    "msglen": ("interaction", "message_length"),
    "emoji": ("interaction", "emoji_use"),
    "tone": ("relational", "tone"),
    "cont": ("relational", "continuity"),
    "freq": ("proactivity", "maximum_frequency"),
}
_SAFETY_PREFERENCES = {
    "safe1": "romantic_language",
    "safe2": "sexual_language",
    # exclusivity_language and guilt_after_no_reply are fixed always-off rules
    # (context_injection); their GUMI_CLASSIC defaults are no longer asked.
}
_MEDIA = {"img": "images", "aud": "audio", "mus": "music"}


@dataclass(frozen=True)
class Assembled:
    baseline: dict
    gumi_profile: dict
    runtime_policy: dict
    checkin_state: dict
    private_env: dict[str, str]
    consents: dict[str, bool]


def _allowed(question_id: str) -> set[str]:
    for question in build_questions():
        if question.id == question_id:
            return {option.value for option in question.options}
    raise KeyError(question_id)


def _choice(answers: Mapping[str, str], question_id: str) -> str | None:
    value = answers.get(question_id)
    if value is None:
        return None
    if value not in _allowed(question_id):
        raise ValueError(f"invalid answer for {question_id}")
    return value


def _yes(answers: Mapping[str, str], question_id: str) -> bool:
    return _choice(answers, question_id) == "yes"


def _battery(answers: Mapping[str, str]) -> dict[str, Any]:
    responses: dict[str, int] = {}
    for question in build_questions():
        if not question.in_battery:
            continue
        value = _choice(answers, question.id)
        if value is not None:
            responses.update(question.item_responses(value))
    return battery_from_responses(responses)


def _preferences(answers: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    preferences = GUMI_CLASSIC.preferences()
    for answer_id, (group, key) in _CHOICE_PREFERENCES.items():
        value = _choice(answers, answer_id)
        if value is not None:
            preferences[group][key] = value
    for answer_id, key in _SAFETY_PREFERENCES.items():
        if _choice(answers, answer_id) is not None:
            preferences["safety"][key] = _yes(answers, answer_id)
    quiet = answers.get("quiet")
    if quiet is not None and quiet != SKIP:
        if not _QUIET_HOURS.fullmatch(quiet):
            raise ValueError("quiet hours must look like HH:MM-HH:MM")
        preferences["proactivity"]["quiet_hours"] = quiet
    return preferences


def _validated_gemini_key(value: str) -> str:
    """Same normalization and format check as the CLI (``UserOnboarding``)."""
    checker = object.__new__(UserOnboarding)  # the check needs no registry or I/O
    checker.gemini_validator = UserOnboarding._default_gemini_validator
    return checker._validated_gemini_key(value)


def assemble(
    answers: Mapping[str, str],
    *,
    telegram_token: str,
    chat_id: str,
    gemini_key: str = "",
    language: str | None = None,
) -> Assembled:
    """Build the arguments of ``materialize_user_artifacts`` from onboarding answers."""
    gemini_key = _validated_gemini_key(gemini_key)
    name = answers.get("name")
    baseline = {
        "display_name": None if name in (None, SKIP) else name,
        "timezone": answers.get("tz"),
        "language": language,
        "battery": _battery(answers),
        # Which wording the answers were collected with (the CLI uses the original text).
        "battery_presentation": BATTERY_PRESENTATION,
    }
    selected = {media: _yes(answers, answer_id) for answer_id, media in _MEDIA.items()}
    media = {
        media: {
            "selected": value,
            "enabled": value and bool(gemini_key),
            **({} if not value or gemini_key else {"remediation": _REMEDIATION}),
        }
        for media, value in selected.items()
    }
    armed = _yes(answers, "armed")
    # Gumi writes first whenever scheduled messages are allowed; otherwise it waits.
    first_message = "gumi_sends_first" if armed else "wait"
    credentials = {"telegram_bot_token": telegram_token, "telegram_target": chat_id}
    if gemini_key:
        credentials["gemini_api_key"] = gemini_key
    return Assembled(
        baseline=_artifact_safe(baseline),
        gumi_profile={
            "flavor": {"id": GUMI_CLASSIC.identifier, "version": GUMI_CLASSIC.version},
            "setup_mode": _choice(answers, "mode") or "default",
            "preferences": _artifact_safe(_preferences(answers)),
            "media": _artifact_safe(media),
        },
        runtime_policy={
            "delivery_capability_enabled": True,
            "delivery_execution_armed": armed,
            "first_message": first_message,
            "credential_references": UserOnboarding._credential_references(credentials),
        },
        checkin_state={
            "recurring": {"status": "armed" if armed else "off"},
            "first_message": {
                "status": "requested" if first_message == "gumi_sends_first" else "waiting"
            },
        },
        private_env=UserOnboarding._private_env(credentials),
        consents={
            "consent_for_active_elicitation": _yes(answers, "c_act"),
            "consent_for_passive_extraction": _yes(answers, "c_pas"),
        },
    )


def _write_consents(directory: Path, consents: Mapping[str, bool]) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("canonical check-in directory is unsafe")
    directory.chmod(0o700)
    path = directory / _CONSENT_FILE
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("canonical delivery policy is unsafe")
    existing: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("canonical delivery policy is invalid")
        existing = loaded
    merged = {**existing, **consents}
    descriptor, temporary = tempfile.mkstemp(prefix=".delivery-policy.", dir=directory)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(merged, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def finalize(registry: ProfileRegistry, user: UserProfile, assembled: Assembled) -> Path:
    """Write the canonical artifacts, then the consent choices (merged, never replaced)."""
    path = registry.materialize_user_artifacts(
        user,
        baseline=assembled.baseline,
        gumi_profile=assembled.gumi_profile,
        runtime_policy=assembled.runtime_policy,
        checkin_state=assembled.checkin_state,
        private_env=assembled.private_env,
    )
    _write_consents(registry.user_home / "checkin", assembled.consents)
    return path
