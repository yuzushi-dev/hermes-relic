"""Telegram wording of the baseline battery ("telegram-v2").

Only the presentation lives here: texts, preambles and scale labels. Item ids,
recorded values, ``reverse_scored`` and the CLI wording stay in ``item_battery``.

TIPI and ECR-RS now use the original validated text:
- TIPI (Gosling, Rentfrow, & Swann, 2003): the ten original single items, asked
  one by one on the agree/disagree scale. License, from the official TIPI page:
  "ANYONE CAN USE IT FOR ANY PURPOSE" — citation requested, given here and in
  the docs.
- ECR-RS (Fraley, Heffernan, Vicary, & Brumbaugh, 2011): the 9-item GLOBAL
  form from Fraley's lab (2014 update of the measures page), declared here; the
  page carries no explicit license clause. The CLI keeps the "this person"
  wording, so the two are comparable only where the text coincides.

The other three screens remain project calibration rewrites.
"""

from __future__ import annotations

from dataclasses import dataclass

from relic.profile._bootstrap_steps import item_battery as ib

# Stored next to the battery so scores can be told apart from the CLI wording.
BATTERY_PRESENTATION = "telegram-v2"

PREAMBLES: dict[str, str] = {
    "big_five": (
        "First, a little about you. Here are ten statements about how you see "
        "yourself. Tap how much you agree with each one, in general, not just "
        "today: I see myself as..."
    ),
    "attachment": (
        "Please read each of the following statements and rate the extent to "
        "which you believe each statement best describes your feelings about "
        "close relationships in general."
    ),
    "interaction_preferences": (
        "Now, how you like to talk. Tell me how much you agree with each one."
    ),
    "relational_comfort": "Tell me what feels comfortable between us.",
    "diegetic_tolerance": "Tell me how my imaginative world should feel to you.",
}


def _scale(*labels: str, start: int = 1) -> dict[int, str]:
    return {start + index: label for index, label in enumerate(labels)}


TOLERANCE_7 = _scale(
    "Very uncomfortable",
    "Uncomfortable",
    "Somewhat uncomfortable",
    "Neutral / not sure",
    "Somewhat comfortable",
    "Comfortable",
    "Very comfortable",
)
IMPORTANCE_7 = _scale(
    "Not important at all",
    "Not very important",
    "A little important",
    "Moderately important",
    "Quite important",
    "Very important",
    "Essential",
)
SEPARATION_7 = _scale(
    "No need to keep them apart",
    "Mostly blended is fine",
    "A little separation",
    "Some separation",
    "Mostly separate",
    "Very separate",
    "Completely separate",
)

# Item id -> (prompt, scale labels). The ten TIPI items are not listed: they keep
# the original text and the agreement scale straight from ``item_battery``.
_SCALES: dict[str, dict[int, str]] = {
    "tolerance_7": TOLERANCE_7,
    "importance_7": IMPORTANCE_7,
    "separation_7": SEPARATION_7,
    "agreement_7": ib.AGREEMENT_7,
}

# ECR-RS, global form (Fraley's lab, 2014 update of the measures page): the
# chat asks about "people/others" in general, not about one specific person.
# Keys, reverse scoring and the agreement scale are unchanged.
_REWRITTEN: dict[str, tuple[str, str]] = {
    "ECRRS_001": ("It helps to turn to people in times of need.", "agreement_7"),
    "ECRRS_002": ("I usually discuss my problems and concerns with others.", "agreement_7"),
    "ECRRS_003": ("I talk things over with people.", "agreement_7"),
    "ECRRS_004": ("I find it easy to depend on others.", "agreement_7"),
    "ECRRS_005": ("I don't feel comfortable opening up to others.", "agreement_7"),
    "ECRRS_006": ("I prefer not to show others how I feel deep down.", "agreement_7"),
    "ECRRS_007": ("I often worry that other people do not really care for me.", "agreement_7"),
    "ECRRS_008": ("I'm afraid that other people may abandon me.", "agreement_7"),
    "ECRRS_009": (
        "I worry that others won't care about me as much as I care about them.",
        "agreement_7",
    ),
    "INT_001": ("I prefer direct answers from you over softened ones.", "agreement_7"),
    "INT_002": ("I'm comfortable with you giving me constructive criticism.", "agreement_7"),
    "INT_003": ("I'd like you to ask before giving me advice.", "agreement_7"),
    "INT_004": ("I like it when our conversations include some humor.", "agreement_7"),
    "INT_005": (
        "I'm comfortable with conversations that stay ambiguous or open-ended.",
        "agreement_7",
    ),
    "INT_006": ("I'd rather we approach emotional topics gradually.", "agreement_7"),
    "INT_007": ("I'd like you to remember the things we leave unfinished.", "agreement_7"),
    "INT_010": (
        "I'd rather get practical help from you than reflect on feelings together.",
        "agreement_7",
    ),
    "REL_001": (
        "How comfortable would you be if I occasionally wrote to you first?",
        "tolerance_7",
    ),
    "REL_004": (
        "How comfortable would you be if I sometimes said no or set a boundary?",
        "tolerance_7",
    ),
    "REL_005": (
        "How comfortable would you be if I didn't always reply right away?",
        "tolerance_7",
    ),
    "REL_007": (
        "How comfortable would you be if I sometimes challenged your take on something?",
        "tolerance_7",
    ),
    "DIE_001": (
        "How comfortable would you be if I had a place where I live?",
        "tolerance_7",
    ),
    "DIE_002": (
        "How comfortable would you be if I talked about my daily routines?",
        "tolerance_7",
    ),
    "DIE_007": (
        'How comfortable would you be if I said things like "I just woke up" '
        'or "I went out today"?',
        "tolerance_7",
    ),
    "DIE_008": (
        "How comfortable would you be if my world evolved over time?",
        "tolerance_7",
    ),
    "DIE_009": (
        "How important is it to you that I feel consistent over time?",
        "importance_7",
    ),
    "DIE_010": (
        "How separate should my world stay from your real-world life?",
        "separation_7",
    ),
}


@dataclass(frozen=True)
class BatteryPrompt:
    """One Telegram battery question and how its answer maps to item responses."""

    screen: str
    prompt: str
    labels: tuple[tuple[int, str], ...]  # (recorded value, button label)
    item_id: str | None = None  # a single item recorded as-is


def battery_prompts() -> list[BatteryPrompt]:
    """The 37 Telegram battery questions, in presentation order.

    Asked: the ten TIPI items (screen ``big_five``, original text) and every
    item in ``_REWRITTEN``. The eleven reader-dependent items dropped from
    ``_REWRITTEN`` are not asked in chat.
    """
    prompts: list[BatteryPrompt] = []
    for item in ib.USER_BASELINE_ITEMS:
        item_id = item["item_id"]
        if item_id in _REWRITTEN:
            text, scale = _REWRITTEN[item_id]
            labels = _SCALES[scale]
        elif item["screen"] == "big_five":  # TIPI: original validated text
            text = item["canonical_text"]
            labels = ib._scale_labels(item["response_scale"])
        else:
            continue  # not asked in chat; conservative default at assembly
        prompts.append(
            BatteryPrompt(item["screen"], text, tuple(labels.items()), item_id=item_id)
        )
    return prompts
