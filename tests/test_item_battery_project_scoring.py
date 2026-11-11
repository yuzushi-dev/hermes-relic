"""Project items honour ``reverse_scored`` (INT_006 measured the opposite of its construct)."""

from __future__ import annotations

import pytest

from relic.profile._bootstrap_steps import item_battery as ib


def _responses(**overrides: int) -> dict[str, int]:
    responses = {item["item_id"]: ib._scale_bounds(item["response_scale"])[2] for item in ib.BOOTSTRAP_ITEM_REGISTRY}
    responses.update(overrides)
    return responses


def test_agreeing_with_gradual_emotional_topics_means_low_intensity_tolerance():
    agree = ib.score_item_battery(_responses(INT_006=7))
    disagree = ib.score_item_battery(_responses(INT_006=1))
    assert agree["project_calibration"]["emotional_intensity_tolerance"] == ib.normalize_1_to_7(1)
    assert disagree["project_calibration"]["emotional_intensity_tolerance"] == ib.normalize_1_to_7(7)
    assert agree["project_calibration"]["emotional_intensity_tolerance"] <= 0.35  # style_hints caution band
    assert disagree["project_calibration"]["emotional_intensity_tolerance"] > 0.35


def test_the_recorded_response_is_the_raw_answer_only_the_score_is_reversed():
    battery = ib.battery_from_responses({"INT_006": 7})
    assert battery["responses"]["INT_006"] == 7
    assert battery["scores"]["project_calibration"]["emotional_intensity_tolerance"] == 0.0


def test_int_006_is_flagged_and_is_the_only_reversed_project_item():
    assert ib.ITEMS_BY_ID["INT_006"]["reverse_scored"] is True
    assert {i["item_id"] for i in ib.PROJECT_ITEMS if i["reverse_scored"]} == {"INT_006"}


def test_reverse_scored_flag_is_honoured_for_any_seven_point_project_item(monkeypatch):
    probe = ib._item(
        "TEST_001", "interaction_preferences", "probe", "probe", "agreement_7",
        "probe_construct", "project_derived_calibration", "test", "test", reverse_scored=True,
    )
    plain = ib._item(
        "TEST_002", "interaction_preferences", "probe", "probe", "agreement_7",
        "plain_construct", "project_derived_calibration", "test", "test",
    )
    monkeypatch.setattr(ib, "PROJECT_ITEMS", [*ib.PROJECT_ITEMS, probe, plain])
    for raw in range(1, 8):
        scores = ib.score_project_items({"TEST_001": raw, "TEST_002": raw})
        assert scores["probe_construct"] == ib.normalize_1_to_7(8 - raw)
        assert scores["plain_construct"] == ib.normalize_1_to_7(raw)


def test_reverse_flag_does_not_touch_non_seven_point_scales(monkeypatch):
    emoji = ib._item(
        "TEST_003", "interaction_preferences", "probe", "probe", "emoji_5",
        "emoji_probe", "project_derived_calibration", "test", "test", reverse_scored=True,
    )
    monkeypatch.setattr(ib, "PROJECT_ITEMS", [*ib.PROJECT_ITEMS, emoji])
    assert ib.score_project_items({"TEST_003": 3})["emoji_probe"] == 3


def test_unflagged_project_items_score_exactly_as_before():
    responses = _responses(INT_001=6, INT_011=4, REL_001=2, REL_010=5, DIE_009=7, PRO_001=3, SAFE_001=1)
    scores = ib.score_project_items(responses)
    for item in ib.PROJECT_ITEMS:
        if item["item_id"] == "INT_006":
            continue
        raw = responses[item["item_id"]]
        scale = item["response_scale"]
        if scale == "frequency_permission_4":
            expected = round(raw / 4, 3)
        elif scale == "yes_no":
            expected = float(raw)
        elif scale == "emoji_5":
            expected = int(raw)
        else:
            expected = ib.normalize_1_to_7(raw)
        assert scores[item["construct"]] == expected, item["item_id"]
        assert type(scores[item["construct"]]) is type(expected), item["item_id"]


@pytest.mark.parametrize("raw", range(1, 8))
def test_int_006_is_symmetric_around_the_midpoint(raw):
    low = ib.score_project_items({"INT_006": raw})["emotional_intensity_tolerance"]
    high = ib.score_project_items({"INT_006": 8 - raw})["emotional_intensity_tolerance"]
    assert round(low + high, 3) == 1.0
