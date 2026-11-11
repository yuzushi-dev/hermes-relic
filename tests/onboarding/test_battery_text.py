"""Telegram presentation of the baseline battery (wording, single-item TIPI, counts)."""

from __future__ import annotations

import pytest

from relic.onboarding import battery_text as bt
from relic.onboarding.assemble import assemble
from relic.onboarding.questions import build_questions
from relic.profile._bootstrap_steps import item_battery as ib

TOKEN = "111111111:" + "A" * 35  # fake, built at runtime
CHAT_ID = "123456789"

# The ten TIPI items are now single items: each trait has one direct and one
# reverse-scored item (asked as its own question, recorded as-is).
TRAIT_ITEMS = {
    "extraversion": ("TIPI_001", "TIPI_006"),
    "agreeableness": ("TIPI_007", "TIPI_002"),
    "conscientiousness": ("TIPI_003", "TIPI_008"),
    "emotional_stability": ("TIPI_009", "TIPI_004"),
    "openness": ("TIPI_005", "TIPI_010"),
}
TRAIT_ORDER = ["extraversion", "agreeableness", "conscientiousness", "emotional_stability", "openness"]
# TIPI reverse-scored items: the even ones, 2, 4, 6, 8, 10.
REVERSE_ITEMS = ("TIPI_002", "TIPI_004", "TIPI_006", "TIPI_008", "TIPI_010")


def _battery():
    return [q for q in build_questions() if q.in_battery]


def _by_item():
    return {q.item_id: q for q in _battery() if q.item_id}


def _answers_for(values: dict[str, int]) -> dict[str, str]:
    """Telegram answers that reproduce the given per-item values."""
    return {q.id: str(values[q.item_id]) for q in _battery() if q.item_id}


def _values(default: int = 4) -> dict[str, int]:
    """Per-item values for every visible item; unasked ones at their defaults."""
    values = {item["item_id"]: default for item in ib.USER_BASELINE_ITEMS}
    asked = {q.item_id for q in _battery() if q.item_id}
    for item in ib.USER_BASELINE_ITEMS:
        if item["item_id"] not in asked:
            values[item["item_id"]] = ib._conservative_default(item)
    return values


# --- (a) single-item TIPI mapping ---------------------------------------------


@pytest.mark.parametrize("trait", TRAIT_ORDER)
@pytest.mark.parametrize("r", range(1, 8))
def test_single_item_choices_score_exactly_the_chosen_value(trait, r):
    questions = _by_item()
    responses: dict[str, int] = {}
    for t in TRAIT_ORDER:
        chosen = r if t == trait else 4
        direct, inverse = TRAIT_ITEMS[t]
        responses.update(questions[direct].item_responses(str(chosen)))
        responses.update(questions[inverse].item_responses(str(8 - chosen)))
    for item in ib.TIPI_ITEMS:
        assert item["item_id"] in responses
    assert ib.score_tipi(responses)[trait] == ib.normalize_1_to_7(r)


def test_tipi_items_are_single_questions_with_reverse_items_2_4_6_8_10():
    questions = _by_item()
    asked_tipi = [q.item_id for q in _battery() if q.item_id and q.item_id.startswith("TIPI_")]
    assert asked_tipi == [f"TIPI_{n:03d}" for n in range(1, 11)]
    # every TIPI question records exactly its own item, as-is
    for item_id in asked_tipi:
        assert questions[item_id].item_responses("2") == {item_id: 2}
    for item_id in REVERSE_ITEMS:
        assert ib.ITEMS_BY_ID[item_id]["reverse_scored"] is True
    for item in ib.TIPI_ITEMS:
        if item["item_id"] not in REVERSE_ITEMS:
            assert item["reverse_scored"] is False


def test_tipi_wording_and_labels():
    questions = [q for q in _battery() if q.item_id and q.item_id.startswith("TIPI_")]
    assert [q.prompt for q in questions] == [
        ib.ITEMS_BY_ID[f"TIPI_{n:03d}"]["canonical_text"] for n in range(1, 11)
    ]
    assert questions[0].prompt == "Extraverted, enthusiastic."
    assert questions[1].prompt == "Critical, quarrelsome."
    assert questions[5].prompt == "Reserved, quiet."
    for question in questions:
        assert [o.label for o in question.options] == list(ib.AGREEMENT_7.values())
        assert [o.value for o in question.options] == [str(n) for n in range(1, 8)]


# --- (b) assembly covers all 48 items --------------------------------------------


def test_assembled_responses_cover_all_48_items_without_unexpected_defaults():
    values = _values()
    values["TIPI_001"] = 6
    values["TIPI_006"] = 2
    values["INT_001"] = 6
    answers = _answers_for(values)
    assert len(answers) == 37
    assembled = assemble(
        {"name": "__skip__", "tz": "Europe/Rome", "mode": "default", **answers},
        telegram_token=TOKEN,
        chat_id=CHAT_ID,
    )
    responses = assembled.baseline["battery"]["responses"]
    visible = {item["item_id"] for item in ib.USER_BASELINE_ITEMS}
    assert len(visible) == 48
    assert {k: responses[k] for k in visible} == values
    # the 19 hidden items keep their conservative defaults
    hidden = {item["item_id"] for item in ib.BOOTSTRAP_ITEM_REGISTRY} - visible
    assert len(hidden) == 19
    for item_id in hidden:
        assert responses[item_id] == ib._conservative_default(ib.ITEMS_BY_ID[item_id])


# --- (c)/(d) shape of every question ----------------------------------------------


def test_every_question_fits_telegram_limits():
    for question in build_questions():
        # the notice screen is the one approved text longer than a plain question
        limit = 200 if question.id == "notice" else 140
        assert len(question.prompt) <= limit, question.id
        assert len(question.options) <= 7, question.id
        labels = [o.label for o in question.options]
        assert len(labels) == len(set(labels)), question.id
        for index in range(len(question.options)):
            assert len(f"rg:{question.id}:{index}".encode()) <= 64


# --- (e) ECR-RS global wording; the CLI wording is untouched ----------------------


# Fraley et al., "Update on Global/General Attachment" (August 2014), verbatim.
ECRRS_GLOBAL_OFFICIAL = {
    "ECRRS_001": "It helps to turn to people in times of need.",
    "ECRRS_002": "I usually discuss my problems and concerns with others.",
    "ECRRS_003": "I talk things over with people.",
    "ECRRS_004": "I find it easy to depend on others.",
    "ECRRS_005": "I don't feel comfortable opening up to others.",
    "ECRRS_006": "I prefer not to show others how I feel deep down.",
    "ECRRS_007": "I often worry that other people do not really care for me.",
    "ECRRS_008": "I'm afraid that other people may abandon me.",
    "ECRRS_009": "I worry that others won't care about me as much as I care about them.",
}


def test_ecrrs_items_use_the_global_wording_and_scale():
    ecr = [q for q in _battery() if q.item_id and q.item_id.startswith("ECRRS_")]
    assert len(ecr) == 9
    for question in ecr:
        assert "this person" not in question.prompt
        assert [o.label for o in question.options] == list(ib.AGREEMENT_7.values())
        assert question.prompt == ECRRS_GLOBAL_OFFICIAL[question.item_id]
    assert [q.item_id for q in ecr] == sorted(ECRRS_GLOBAL_OFFICIAL)
    # the CLI keeps the per-person wording
    assert ib.ITEMS_BY_ID["ECRRS_009"]["canonical_text"] == (
        "I worry that this person won't care about me as much as I care about him or her."
    )


def test_cli_battery_texts_are_untouched():
    assert ib.ITEMS_BY_ID["TIPI_001"]["canonical_text"] == "Extraverted, enthusiastic."
    assert ib.ITEMS_BY_ID["INT_001"]["canonical_text"] == (
        "I prefer direct answers over softened answers."
    )
    assert ib.TOLERANCE_7[2] == "Slightly uncomfortable"
    assert ib.USER_SCREEN_PREAMBLES["attachment"] == (
        "Think about an important close relationship as you answer."
    )
    assert len(ib.USER_BASELINE_ITEMS) == 48


# --- (g) count, ids, preambles ------------------------------------------------------


def test_battery_has_37_questions_with_sequential_ids():
    battery = _battery()
    assert len(battery) == 37
    assert [q.id for q in battery] == [f"b{n:02d}" for n in range(1, 38)]
    assert not any(q.id == "b38" for q in build_questions())


def test_five_preambles_exactly_as_approved():
    preambles = [q.preamble for q in _battery() if q.preamble]
    assert preambles == [
        "First, a little about you. Here are ten statements about how you see "
        "yourself. Tap how much you agree with each one, in general, not just "
        "today: I see myself as...",
        "Please read each of the following statements and rate the extent to "
        "which you believe each statement best describes your feelings about "
        "close relationships in general.",
        "Now, how you like to talk. Tell me how much you agree with each one.",
        "Tell me what feels comfortable between us.",
        "Tell me how my imaginative world should feel to you.",
    ]
    first_of_screen = [q.id for q in _battery() if q.preamble]
    assert first_of_screen == ["b01", "b11", "b20", "b28", "b32"]


# --- wording of the rewritten items and scales --------------------------------------


def test_rewritten_items_use_the_approved_text():
    by_item = _by_item()
    expected = {
        "INT_001": "I prefer direct answers from you over softened ones.",
        "INT_006": "I'd rather we approach emotional topics gradually.",
        "REL_001": "How comfortable would you be if I occasionally wrote to you first?",
        "DIE_007": (
            'How comfortable would you be if I said things like "I just woke up" '
            'or "I went out today"?'
        ),
        "DIE_009": "How important is it to you that I feel consistent over time?",
        "DIE_010": "How separate should my world stay from your real-world life?",
    }
    for item_id, text in expected.items():
        assert by_item[item_id].prompt == text, item_id


def test_reader_dependendent_items_are_not_asked_in_chat():
    """The 11 items without a chat reader stay out of the battery, with defaults."""
    asked = {q.item_id for q in _battery()}
    unasked = {
        item["item_id"] for item in ib.USER_BASELINE_ITEMS
    } - asked
    assert unasked == {
        "INT_008", "INT_009", "INT_011",
        "REL_003", "REL_006", "REL_008", "REL_010",
        "DIE_003", "DIE_004", "DIE_005", "DIE_006",
    }
    assert len(asked) == 37


def test_scale_labels_are_the_approved_ones():
    by_item = _by_item()

    def labels(item_id):
        return [o.label for o in by_item[item_id].options]

    assert labels("REL_001") == [
        "Very uncomfortable",
        "Uncomfortable",
        "Somewhat uncomfortable",
        "Neutral / not sure",
        "Somewhat comfortable",
        "Comfortable",
        "Very comfortable",
    ]
    assert labels("DIE_001") == labels("REL_001")
    assert labels("DIE_009") == [
        "Not important at all",
        "Not very important",
        "A little important",
        "Moderately important",
        "Quite important",
        "Very important",
        "Essential",
    ]
    assert labels("DIE_010") == [
        "No need to keep them apart",
        "Mostly blended is fine",
        "A little separation",
        "Some separation",
        "Mostly separate",
        "Very separate",
        "Completely separate",
    ]
    assert labels("INT_001") == list(ib.AGREEMENT_7.values())


def test_scales_keep_the_direction_of_the_recorded_values():
    by_item = _by_item()
    for item_id in ("REL_001", "DIE_001", "DIE_009", "DIE_010"):
        assert [o.value for o in by_item[item_id].options] == [str(n) for n in range(1, 8)]


def test_docs_describe_the_37_question_battery_and_its_validity():
    from pathlib import Path

    docs = (Path(__file__).parents[2] / "docs" / "onboarding-fields.md").read_text(encoding="utf-8")
    assert "37 questions" in docs and "`b01`..`b37`" in docs
    assert "43 questions" not in docs and "`b01`..`b43`" not in docs
    assert "b38" in docs  # old ids are explicitly declared dropped
    assert "telegram-v2" in docs and "telegram-v1" not in docs
    # The validated instruments are now the original text.
    assert "the original validated instrument text" in docs or "is the\noriginal validated instrument text" in docs
    assert "Gosling, Rentfrow, & Swann, 2003" in docs
    assert "Fraley, Heffernan, Vicary, &" in docs
    assert "ANYONE CAN USE IT FOR" in docs  # license text, wrapped across lines
    assert "NO NEED TO ASK ME FOR PERMISSION" in docs
    assert "no explicit license clause" in docs
    # The other screens remain project calibration, and the docs say so.
    assert "project calibration" in docs
    assert "not a clinical\ninstrument" in docs
    # Fixed, always-off rules are documented as such.
    assert "fixed always-off rule" in docs
