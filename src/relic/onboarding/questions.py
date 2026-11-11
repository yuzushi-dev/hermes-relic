"""Declarative question catalog for the Telegram onboarding."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Literal, Mapping
from zoneinfo import ZoneInfo

from relic.onboarding import battery_text

Answers = Mapping[str, str]


def _always(answers: Answers) -> bool:
    return True


@dataclass(frozen=True)
class Option:
    label: str
    value: str


@dataclass(frozen=True)
class Question:
    id: str
    kind: Literal["choice", "text"]
    prompt: str
    options: tuple[Option, ...] = ()
    when: Callable[[Answers], bool] = _always
    # Normalizes the raw text, or returns None when it is invalid.
    validate: Callable[[str], str | None] | None = None
    # Label of the "skip" button for optional open questions.
    skip_label: str | None = None
    # Battery only: the original item id (for example TIPI_001).
    item_id: str | None = None
    # Text shown before the first question of a battery screen.
    preamble: str | None = None
    # Error shown when ``validate`` rejects the text (default: generic message).
    invalid_message: str | None = None

    @property
    def in_battery(self) -> bool:
        return self.item_id is not None

    def item_responses(self, value: str) -> dict[str, int]:
        """The battery item values written by an answer to this question."""
        number = int(value)
        if self.item_id is not None:
            return {self.item_id: number}
        return {}


def _tz(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    try:
        ZoneInfo(value)
    except Exception:
        return None
    return value


_WELCOME_EMOJI = "👋"
_NAME_MAX = 60
_NAME_FORBIDDEN = {"Cc", "Cf", "Zl", "Zp"}
_NAME_ERROR = "Please send a short single-line name, up to 60 characters, or press Skip."


def _name(raw: str) -> str | None:
    value = raw.strip()
    if not value or len(value) > _NAME_MAX:
        return None
    if any(unicodedata.category(ch) in _NAME_FORBIDDEN for ch in value):
        return None
    return value


_QUIET = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d")


def _quiet(raw: str) -> str | None:
    value = raw.strip()
    return value if _QUIET.fullmatch(value) else None


def _yes_no() -> tuple[Option, ...]:
    return (Option("Yes", "yes"), Option("No", "no"))


def _no_yes() -> tuple[Option, ...]:
    # The safest answer comes first for opt-in questions.
    return (Option("No", "no"), Option("Yes", "yes"))


def _recommended_yes_no() -> tuple[Option, ...]:
    # Yes is recommended but never preselected: it counts only when the user taps it.
    return (Option("Yes (recommended)", "yes"), Option("No", "no"))


def _custom(answers: Answers) -> bool:
    return answers.get("mode") == "custom"


def _armed_custom(answers: Answers) -> bool:
    return _custom(answers) and answers.get("armed") == "yes"


def build_questions() -> list[Question]:
    qs: list[Question] = [
        Question(
            "notice",
            "choice",
            "Before we start: I'm Gumi, a fictional character. No person reads "
            "this chat. I'm not a medical or clinical service. In an emergency, "
            "call 112 or your local emergency services.",
            (Option("Continue", "ok"),),
        ),
        Question(
            "name",
            "text",
            f"Nice to meet you! I'm Gumi {_WELCOME_EMOJI}, who are you?",
            skip_label="Skip",
            validate=_name,
            invalid_message=_NAME_ERROR,
        ),
        Question(
            "tz",
            "text",
            "Which time zone are you in? Send an IANA name, for example Europe/Rome.",
            validate=_tz,
        ),
        Question(
            "mode",
            "choice",
            "Choose your setup.",
            (Option("Quick (recommended)", "default"), Option("Custom", "custom")),
        ),
    ]
    seen_screen = None
    for index, entry in enumerate(battery_text.battery_prompts(), start=1):
        preamble = None
        if entry.screen != seen_screen:
            seen_screen = entry.screen
            preamble = battery_text.PREAMBLES[seen_screen]
        qs.append(
            Question(
                f"b{index:02d}",
                "choice",
                entry.prompt,
                tuple(Option(label, str(value)) for value, label in entry.labels),
                item_id=entry.item_id,
                preamble=preamble,
            )
        )
    qs += [
        Question(
            "msglen",
            "choice",
            "Message length?",
            (Option("Brief", "brief"), Option("Standard", "standard"), Option("Detailed", "detailed")),
            when=_custom,
        ),
        Question(
            "emoji",
            "choice",
            "Emoji?",
            (Option("None", "none"), Option("Light", "light"), Option("Frequent", "frequent")),
            when=_custom,
        ),
        Question(
            "tone",
            "choice",
            "Tone?",
            (Option("Warm", "warm"), Option("Neutral", "neutral"), Option("Direct", "direct")),
            when=_custom,
        ),
        Question(
            "cont",
            "choice",
            "Should I remember earlier conversations?",
            (Option("Only when you ask (opt-in)", "opt_in"), Option("Never", "always_off")),
            when=_custom,
        ),
        Question("safe1", "choice", "Is romantic language okay with you?", _no_yes(), when=_custom),
        Question("safe2", "choice", "Is sexual language okay with you?", _no_yes(), when=_custom),
        # exclusivity_language and guilt_after_no_reply are fixed rules, always
        # off; the defaults come from GUMI_CLASSIC.preferences() and are no
        # longer asked.
        Question("img", "choice", "Can I send you some pics of me sometimes?", _yes_no()),
        Question("aud", "choice", "Can I send you voice notes sometimes?", _yes_no()),
        Question("mus", "choice", "Can I send you some songs I write sometimes?", _yes_no()),
        Question(
            "armed",
            "choice",
            "Can I write to you here on my own (scheduled check-ins)?",
            _yes_no(),
        ),
        Question(
            "freq",
            "choice",
            "Maximum frequency?",
            (Option("1 per day", "1/day"), Option("2 per day", "2/day"), Option("3 per week", "3/week")),
            when=_armed_custom,
        ),
        Question(
            "quiet",
            "text",
            "Quiet hours as HH:MM-HH:MM.",
            validate=_quiet,
            skip_label="Keep 22:00-08:00",
            when=_armed_custom,
        ),
        Question(
            "c_act",
            "choice",
            "May I ask you questions on my own to get to know you better? "
            "(You can change this later.)",
            _recommended_yes_no(),
        ),
        Question(
            "c_pas",
            "choice",
            "May I learn from our chats? Local classifiers by default; opt-in hosts (Ollama Cloud/Google media) receive text. See privacy notice.",
            _recommended_yes_no(),
            preamble=(
                "Privacy notice: remote classifier opt-in sends chat excerpts to your chosen host "
                "(including Ollama Cloud, ollama.com). Separately enabled Google media "
                "(Gemini/Lyria, generativelanguage.googleapis.com) may receive text and recent "
                "chat/memory context. This consent does not enable cloud media."
            ),
        ),
        Question(
            "confirm",
            "choice",
            "__summary__",
            (Option("Save", "save"), Option("Start over", "restart")),
        ),
    ]
    return qs
