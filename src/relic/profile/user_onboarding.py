"""Default-first setup for one local User."""

from __future__ import annotations

import hashlib
import locale
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from relic.profile._bootstrap_steps.item_battery import collect_user_baseline_battery
from relic.profile.registry import ProfileRegistry, UserProfile


_TELEGRAM_TOKEN = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_TARGET = re.compile(r"^-?\d+$")
_GEMINI_API_KEY = re.compile(r"^AIza[A-Za-z0-9_-]{20,}$")
_REMEDIATION = "Add a Gemini API key with relic-gumi configure."
_QUIET_HOURS = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class FlavorSpec:
    """Versioned defaults for an out-of-the-box Gumi experience."""

    identifier: str
    version: int
    interaction_defaults: Mapping[str, Any]
    relational_defaults: Mapping[str, Any]
    safety_defaults: Mapping[str, Any]
    proactivity_defaults: Mapping[str, Any]

    def preferences(self) -> dict[str, dict[str, Any]]:
        return {
            "interaction": dict(self.interaction_defaults),
            "relational": dict(self.relational_defaults),
            "safety": dict(self.safety_defaults),
            "proactivity": dict(self.proactivity_defaults),
        }


GUMI_CLASSIC = FlavorSpec(
    identifier="gumi-classic",
    version=1,
    interaction_defaults={
        "message_length": "brief",
        "emoji_use": "light",
        "reply_pace": "natural",
    },
    relational_defaults={
        "tone": "warm",
        "continuity": "opt_in",
        "role": "supportive_companion",
    },
    safety_defaults={
        "romantic_language": False,
        "sexual_language": False,
        "exclusivity_language": False,
        "guilt_after_no_reply": False,
        "outside_support": "encourage_when_needed",
    },
    proactivity_defaults={
        "recurring_messages": "on",
        "after_no_reply": "pause",
        "quiet_hours": "22:00-08:00",
        "maximum_frequency": "2/day",
    },
)


def _locale_language() -> str | None:
    try:
        locale_name = locale.getlocale()[0]
    except (TypeError, ValueError):
        return None
    if not locale_name or locale_name.upper() in {"C", "POSIX"}:
        return None
    return locale_name.replace("-", "_").split("_", 1)[0].lower() or None


@dataclass(frozen=True)
class OnboardingResult:
    user: UserProfile
    configuration_path: Path
    media: Mapping[str, Mapping[str, Any]]
    delivery_capability_enabled: bool
    delivery_execution_armed: bool
    first_message: str


_FORBIDDEN_ARTIFACT_FIELDS = frozenset(
    {"subject", "experiment", "researcher", "consent", "emergency", "risk"}
)


def _artifact_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _artifact_safe(item)
            for key, item in value.items()
            if isinstance(key, str)
            and not any(term in key.lower() for term in _FORBIDDEN_ARTIFACT_FIELDS)
        }
    if isinstance(value, list):
        return [_artifact_safe(item) for item in value]
    return value


def load_onboarding_state(user_home: Path) -> dict[str, Any] | None:
    """Load the public configuration assembled from canonical User artifacts."""
    user_home = Path(user_home)
    return ProfileRegistry(relic_home=user_home.parent).load_user_configuration()


class _RestartAtMode(Exception):
    pass


class _Cancelled(Exception):
    pass


class UserOnboarding:
    """Collect and atomically save one User's setup choices."""

    def __init__(
        self,
        registry: ProfileRegistry,
        *,
        io_in: TextIO | None = None,
        io_out: TextIO | None = None,
        environ: Mapping[str, str] | None = None,
        gemini_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.io_in = io_in or sys.stdin
        self.io_out = io_out or sys.stdout
        self.environ = environ if environ is not None else os.environ
        self.gemini_validator = (
            gemini_validator if gemini_validator is not None else self._default_gemini_validator
        )
        self._interrupt_count = 0

    def run(self) -> OnboardingResult | None:
        user = self.registry.initialize_user()
        saved = self.registry.load_user_configuration() or {}
        saved_credentials = self._saved_credentials()
        while True:
            try:
                mode = self._choose_mode()
                self._choose_flavor()
                baseline = self._collect_baseline()
                preferences = self._preferences(saved)
                if mode == "custom":
                    self._collect_advanced_preferences(preferences)
                media, credentials = self._collect_media(saved_credentials)
                delivery = self._collect_telegram(credentials, saved_credentials)
                if mode == "custom" and delivery["delivery_capability_enabled"]:
                    self._collect_delivery_preferences(preferences)
                first_message = self._choose_first_message()
                state = {
                    "baseline": _artifact_safe(baseline),
                    "flavor": {"id": GUMI_CLASSIC.identifier, "version": GUMI_CLASSIC.version},
                    "setup_mode": mode,
                    "preferences": _artifact_safe(preferences),
                    "media": _artifact_safe(media),
                    "delivery_capability_enabled": delivery["delivery_capability_enabled"],
                    "delivery_execution_armed": delivery["delivery_execution_armed"],
                    "first_message": first_message,
                }
                path = self.registry.materialize_user_artifacts(
                    user,
                    baseline=state["baseline"],
                    gumi_profile={
                        name: state[name]
                        for name in ("flavor", "setup_mode", "preferences", "media")
                    },
                    runtime_policy={
                        name: state[name]
                        for name in (
                            "delivery_capability_enabled",
                            "delivery_execution_armed",
                            "first_message",
                        )
                    }
                    | {"credential_references": self._credential_references(credentials)},
                    checkin_state={
                        "recurring": {
                            "status": (
                                "armed" if state["delivery_execution_armed"] else "off"
                            )
                        },
                        "first_message": {
                            "status": (
                                "requested"
                                if first_message == "gumi_sends_first"
                                else "waiting"
                            )
                        },
                    },
                    private_env=self._private_env(credentials),
                )
                self._render_summary(state)
                return OnboardingResult(
                    user=user,
                    configuration_path=path,
                    media=state["media"],
                    delivery_capability_enabled=state["delivery_capability_enabled"],
                    delivery_execution_armed=state["delivery_execution_armed"],
                    first_message=first_message,
                )
            except _RestartAtMode:
                self._print("Your current choices were discarded. Returning to setup selection.")
                self._interrupt_count = 0
            except _Cancelled:
                self._print("Setup cancelled. Nothing was saved.")
                return None
    def _choose_mode(self) -> str:
        while True:
            try:
                value = self._ask(
                    "Choose your setup: [D]efault (recommended) / [C]ustom / [Q]uit", "d"
                ).lower()
            except _RestartAtMode:
                raise _Cancelled
            if value in {"d", "default"}:
                return "default"
            if value in {"c", "custom"}:
                return "custom"
            if value in {"q", "quit"}:
                raise _Cancelled
            self._print("Choose Default, Custom, or Quit.")

    def _choose_flavor(self) -> None:
        while True:
            value = self._ask("Choose Gumi [C]lassic", "c").lower()
            if value in {"c", "classic"}:
                return
            if value in {"q", "quit"}:
                raise _Cancelled
            self._print("Gumi Classic is the available choice for you.")

    def _collect_baseline(self) -> dict[str, Any]:
        return {
            "display_name": None,
            "timezone": None,
            "language": _locale_language(),
            "battery": collect_user_baseline_battery(
                self.io_in,
                self.io_out,
                ask=self._ask,
            ),
        }

    def _collect_advanced_preferences(self, preferences: dict[str, dict[str, Any]]) -> None:
        if self._confirm("Customize message style? [y/N]", default=False):
            preferences["interaction"]["message_length"] = self._choice(
                "Message length [brief/standard/detailed]", {"brief", "standard", "detailed"}, "brief"
            )
            preferences["interaction"]["emoji_use"] = self._choice(
                "Emoji use [none/light/frequent]", {"none", "light", "frequent"}, "light"
            )
        if self._confirm("Customize conversation style? [y/N]", default=False):
            preferences["relational"]["tone"] = self._choice(
                "Tone [warm/neutral/direct]", {"warm", "neutral", "direct"}, "warm"
            )
            preferences["relational"]["continuity"] = self._choice(
                "Continuity [opt_in/always_off]", {"opt_in", "always_off"}, "opt_in"
            )
        if self._confirm("Customize conversation limits? [y/N]", default=False):
            preferences["safety"]["romantic_language"] = self._confirm(
                "Allow romantic language? [y/N]", default=False
            )
            preferences["safety"]["sexual_language"] = self._confirm(
                "Allow sexual language? [y/N]", default=False
            )
            # exclusivity_language and guilt_after_no_reply are fixed always-off rules,
            # not asked (they keep their False default above).

    def _collect_media(
        self, saved_credentials: Mapping[str, str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        selected = {
            "images": self._confirm("Enable images? [y/N]", default=False),
            "audio": self._confirm("Enable audio? [y/N]", default=False),
            "music": self._confirm("Enable music? [y/N]", default=False),
        }
        credentials = dict(saved_credentials)
        key = ""
        if any(selected.values()):
            key = self._gemini_key(saved_credentials)
            if not key:
                key = self._validated_gemini_key(
                    self._ask("Gemini API key (leave blank to keep selected media off)")
                )
            if key:
                credentials["gemini_api_key"] = key
        media = {
            name: {
                "selected": selected_value,
                "enabled": selected_value and bool(key),
                **({} if not selected_value or key else {"remediation": _REMEDIATION}),
            }
            for name, selected_value in selected.items()
        }
        return media, credentials

    def _collect_telegram(
        self, credentials: dict[str, str], saved_credentials: Mapping[str, str]
    ) -> dict[str, bool]:
        token, target, preconfirmed = self._telegram_values(saved_credentials)
        if token and not preconfirmed and not self._confirm(
            f"Use detected Telegram target ending in {self._masked_telegram_target(target)}? [Y/n]",
            default=True,
        ):
            return {"delivery_capability_enabled": False, "delivery_execution_armed": False}
        if not token:
            token = self._ask("Telegram bot token (leave blank to keep delivery off)")
            if not self._valid_telegram_token(token):
                self._print(
                    "Your Telegram delivery remains off. Add a valid token and numeric target with relic-gumi configure."
                )
                return {"delivery_capability_enabled": False, "delivery_execution_armed": False}
        if not target:
            target = self._ask("Telegram target (numbers only; leave blank to keep delivery off)")
            if not self._valid_telegram_target(target):
                self._print(
                    "Your Telegram delivery remains off. Add a valid token and numeric target with relic-gumi configure."
                )
                return {"delivery_capability_enabled": False, "delivery_execution_armed": False}
        credentials["telegram_bot_token"] = token
        credentials["telegram_target"] = target
        armed = self._confirm(
            "Arm recurring Telegram messages? They can arrive without opening Gumi. [Y/n]",
            default=True,
        )
        return {"delivery_capability_enabled": True, "delivery_execution_armed": armed}

    def _collect_delivery_preferences(self, preferences: dict[str, dict[str, Any]]) -> None:
        if not self._confirm("Customize recurring delivery? [y/N]", default=False):
            return
        preferences["proactivity"]["maximum_frequency"] = self._choice(
            "Maximum frequency [1/day/2/day/3/week]", {"1/day", "2/day", "3/week"}, "2/day"
        )
        while True:
            quiet_hours = self._ask("Quiet hours [22:00-08:00]", "22:00-08:00")
            if _QUIET_HOURS.fullmatch(quiet_hours):
                preferences["proactivity"]["quiet_hours"] = quiet_hours
                return
            self._print("Enter quiet hours as HH:MM-HH:MM.")

    def _choose_first_message(self) -> str:
        while True:
            value = self._ask("First message [G]umi sends first / [W]ait for me", "g").lower()
            if value in {"g", "gumi", "send"}:
                return "gumi_sends_first"
            if value in {"w", "wait"}:
                return "wait"
            self._print("Choose Gumi or Wait.")

    @staticmethod
    def _preferences(saved: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        preferences = GUMI_CLASSIC.preferences()
        saved_preferences = saved.get("preferences")
        if not isinstance(saved_preferences, dict):
            return preferences
        for name, values in saved_preferences.items():
            if isinstance(name, str) and isinstance(values, dict):
                preferences.setdefault(name, {}).update(values)
        return preferences

    def _saved_credentials(self) -> dict[str, str]:
        if self.registry.load_user_configuration() is None:
            return {}
        values = self._read_env(self.registry.hermes_user_env_path)
        credentials: dict[str, str] = {}
        gemini = self._validated_gemini_key(values.get("GEMINI_API_KEY", ""))
        if gemini:
            credentials["gemini_api_key"] = gemini
        token, target = self._telegram_pair(values)
        if token:
            credentials["telegram_bot_token"] = token
            credentials["telegram_target"] = target
        return credentials

    @staticmethod
    def _private_env(credentials: Mapping[str, str]) -> dict[str, str]:
        names = {
            "gemini_api_key": "GEMINI_API_KEY",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_target": "TELEGRAM_HOME_CHANNEL",
        }
        return {
            env_name: credentials[name]
            for name, env_name in names.items()
            if isinstance(credentials.get(name), str) and credentials[name]
        }

    @classmethod
    def _credential_references(cls, credentials: Mapping[str, str]) -> dict[str, dict[str, str]]:
        return {
            name: {
                "location": "hermes_profile:.env",
                "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
            for name, value in cls._private_env(credentials).items()
        }

    def _gemini_key(self, saved_credentials: Mapping[str, str]) -> str:
        for values in self._canonical_sources(saved_credentials):
            key = self._validated_gemini_key(
                values.get("GEMINI_API_KEY") or values.get("gemini_api_key", "")
            )
            if key:
                return key
        return ""

    def _telegram_values(self, saved_credentials: Mapping[str, str]) -> tuple[str, str, bool]:
        for values, preconfirmed in (
            (saved_credentials, True),
            (self.environ, False),
            (self._read_env(self.registry.hermes_user_home / ".env"), False),
        ):
            token, target = self._telegram_pair(values)
            if token:
                return token, target, preconfirmed
        return "", "", False

    def _telegram_pair(self, values: Mapping[str, str]) -> tuple[str, str]:
        token = self._normalized_credential(
            values.get("TELEGRAM_BOT_TOKEN") or values.get("telegram_bot_token", "")
        )
        target = self._normalized_credential(
            values.get("TELEGRAM_HOME_CHANNEL")
            or values.get("TELEGRAM_ALLOWED_USERS")
            or values.get("telegram_target", "")
        )
        if self._valid_telegram_token(token) and self._valid_telegram_target(target):
            return token, target
        return "", ""

    @staticmethod
    def _normalized_credential(value: object) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value

    @staticmethod
    def _default_gemini_validator(value: str) -> bool:
        return bool(_GEMINI_API_KEY.fullmatch(value))

    def _validated_gemini_key(self, value: object) -> str:
        key = self._normalized_credential(value)
        return key if key and self.gemini_validator(key) else ""

    @staticmethod
    def _masked_telegram_target(target: str) -> str:
        return f"…{target[-4:]}" if len(target) > 4 else "a configured target"

    def _canonical_sources(
        self, saved_credentials: Mapping[str, str]
    ) -> tuple[Mapping[str, str], ...]:
        return (
            self.environ,
            saved_credentials,
            self._read_env(self.registry.hermes_user_home / ".env"),
        )

    @staticmethod
    def _read_env(path: Path) -> dict[str, str]:
        if path.is_symlink() or not path.is_file():
            return {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        values: dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export") and len(line) > 6 and line[6].isspace():
                line = line[6:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
        return values

    @staticmethod
    def _valid_telegram_token(value: str) -> bool:
        return bool(_TELEGRAM_TOKEN.fullmatch(value.strip()))

    @staticmethod
    def _valid_telegram_target(value: str) -> bool:
        return bool(_TELEGRAM_TARGET.fullmatch(value.strip()))

    def _choice(self, prompt: str, choices: set[str], default: str) -> str:
        while True:
            value = self._ask(prompt, default).lower()
            if value in choices:
                return value
            self._print(f"Choose one of: {', '.join(sorted(choices))}.")

    def _confirm(self, prompt: str, *, default: bool) -> bool:
        while True:
            value = self._ask(prompt).lower()
            if not value:
                return default
            if value in {"y", "yes"}:
                return True
            if value in {"n", "no"}:
                return False
            self._print("Please answer yes or no.")

    def _ask(self, prompt: str, default: str = "") -> str:
        while True:
            self._print(prompt)
            try:
                raw = self.io_in.readline()
            except KeyboardInterrupt:
                self._interrupt_count += 1
                if self._interrupt_count == 1:
                    self._print("Press Ctrl+C again to discard your current choices.")
                    continue
                raise _RestartAtMode
            except (EOFError, OSError):
                raise _Cancelled
            if not raw:
                raise _Cancelled
            self._interrupt_count = 0
            value = raw.strip()
            return value or default

    def _render_summary(self, state: Mapping[str, Any]) -> None:
        self._print("\nGumi Classic is configured for you.")
        for name in ("images", "audio", "music"):
            media = state["media"][name]
            if media["enabled"]:
                self._print(f"You enabled {name}.")
            elif media["selected"]:
                self._print(f"You selected {name}; add your Gemini API key to turn it on.")
            else:
                self._print(f"You left {name} off.")
        if state["delivery_capability_enabled"]:
            status = "armed" if state["delivery_execution_armed"] else "ready but not armed"
            self._print(f"Your recurring Telegram preference is {status}.")
        else:
            self._print("Your recurring Telegram preference is off.")
        if state["first_message"] == "gumi_sends_first":
            self._print("You requested that Gumi send your first message.")
        else:
            self._print("You chose to wait for your first message.")
        self._print("You can change these choices any time: relic-gumi configure")

    def _print(self, message: str) -> None:
        print(message, file=self.io_out)
