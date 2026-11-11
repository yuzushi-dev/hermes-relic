from __future__ import annotations

from relic.onboarding.battery_text import BATTERY_PRESENTATION
from relic.onboarding.engine import Engine
from relic.onboarding.questions import build_questions


def test_first_questions_are_notice_then_name_and_text_answer_to_choice_is_rejected():
    e = Engine(build_questions(), {})
    assert e.current().id == "notice"
    assert e.answer("notice", "hello typed").ok is False
    assert e.answer("notice", "ok").ok is True
    assert e.current().id == "name"
    assert e.answer("name", "Ada").ok is True
    assert e.answer("tz", "Europe/Rome").ok is True
    assert e.current().id == "mode"
    # a choice question accepts only an option value
    assert e.answer("mode", "hello typed").ok is False


def test_custom_branch_only_after_mode_custom():
    e = Engine(build_questions(), {})
    for qid, val in [
        ("notice", "ok"),
        ("name", "__skip__"),
        ("tz", "Europe/Rome"),
        ("mode", "default"),
    ]:
        assert e.answer(qid, val).ok
    ids = [q.id for q in e.remaining()]
    assert "msglen" not in ids and "b01" in ids


def test_invalid_timezone_is_rejected_valid_normalized():
    e = Engine(build_questions(), {})
    e.answer("notice", "ok")
    e.answer("name", "__skip__")
    assert e.answer("tz", "Mars/Olympus").ok is False
    assert e.answer("tz", " Europe/Rome ").ok is True
    assert e.answers["tz"] == "Europe/Rome"


def test_progress_saved_with_old_ids_is_ignored_without_error():
    # An older progress file: the removed "welcome" question and a b38..b48 id.
    old = {
        "welcome": "start",
        "tz": "Europe/Rome",
        "name": "Ada",
        "mode": "default",
        "b01": "3",  # meant a different question on the old battery
        "b02": "5",
        "b48": "4",
    }
    e = Engine(build_questions(), old)
    assert "welcome" not in e.answers and "b48" not in e.answers
    assert not any(k.startswith("b") for k in e.answers)
    # known non-battery answers survive and the conversation simply resumes
    assert e.answers == {"tz": "Europe/Rome", "name": "Ada", "mode": "default"}
    assert e.current().id == "notice"
    assert e.progress_in_battery() == (0, 37)


def test_partial_v1_progress_with_only_valid_ids_is_discarded():
    # telegram-v1 save: b01..b20 are all valid ids in v2 but meant other questions.
    old = {"notice": "ok", "name": "Ada", "tz": "Europe/Rome", "mode": "default"}
    old.update({f"b{n:02d}": "3" for n in range(1, 21)})
    for catalog in (None, "telegram-v1"):
        e = Engine(build_questions(), old, catalog)
        assert e.progress_in_battery() == (0, 37)
        assert e.answers == {"notice": "ok", "name": "Ada", "tz": "Europe/Rome", "mode": "default"}


def test_progress_from_the_current_catalog_keeps_battery_answers():
    answers = {"notice": "ok", "b01": "3", "b02": "5"}
    e = Engine(build_questions(), answers, BATTERY_PRESENTATION)
    assert e.answers == answers


def test_progress_with_only_current_ids_is_kept_untouched():
    answers = {"notice": "ok", "name": "Ada", "tz": "Europe/Rome", "mode": "default", "b01": "3"}
    assert Engine(build_questions(), answers).answers == answers


def test_progress_counts_battery():
    e = Engine(
        build_questions(),
        {"notice": "ok", "name": "__skip__", "tz": "Europe/Rome", "mode": "default"},
    )
    done, total = e.progress_in_battery()
    assert (done, total) == (0, 37)
