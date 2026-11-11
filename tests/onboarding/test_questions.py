from __future__ import annotations

from relic.onboarding.questions import build_questions
from relic.profile._bootstrap_steps import item_battery as ib


def test_battery_is_37_in_five_screens_and_ids_fit_callback_limit():
    qs = build_questions()
    battery = [q for q in qs if q.id.startswith("b")]
    assert len(battery) == 37
    assert sum(1 for q in battery if q.preamble) == 5
    for q in qs:
        assert len(f"rg:{q.id}:{len(q.options)}".encode()) <= 64


def test_ids_are_unique():
    ids = [q.id for q in build_questions()]
    assert len(ids) == len(set(ids))


def test_choice_questions_have_options_and_text_have_none():
    for q in build_questions():
        if q.kind == "choice":
            assert len(q.options) >= 1
        else:
            assert q.options == ()


def test_scale_labels_come_from_item_battery():
    qs = {q.id: q for q in build_questions()}
    # b01 is the first TIPI item: original wording, agreement scale from item_battery
    assert qs["b01"].item_id == "TIPI_001"
    assert [o.label for o in qs["b01"].options] == list(ib.AGREEMENT_7.values())
    assert qs["b01"].prompt == ib.ITEMS_BY_ID["TIPI_001"]["canonical_text"]
    # b11 is the first ECR-RS item: global wording, agreement scale from item_battery
    assert qs["b11"].item_id == "ECRRS_001"
    assert [o.label for o in qs["b11"].options] == list(ib.AGREEMENT_7.values())
    assert qs["b11"].prompt == "It helps to turn to people in times of need."


def test_safety_questions_are_custom_only_with_no_first():
    qs = {q.id: q for q in build_questions()}
    # safe3/safe4 were replaced by fixed always-off rules and are no longer asked
    assert "safe3" not in qs and "safe4" not in qs
    for qid in ("safe1", "safe2"):
        q = qs[qid]
        assert [o.value for o in q.options] == ["no", "yes"]
        assert q.options[0].label == "No"
        assert q.when({"mode": "custom"}) is True
        assert q.when({"mode": "default"}) is False


def _q(qid):
    return next(q for q in build_questions() if q.id == qid)


def test_media_optins_are_friendly_requests_yes_first():
    expected = {
        "img": "Can I send you some pics of me sometimes?",
        "aud": "Can I send you voice notes sometimes?",
        "mus": "Can I send you some songs I write sometimes?",
    }
    for qid, text in expected.items():
        q = _q(qid)
        assert q.prompt == text
        assert [(o.label, o.value) for o in q.options] == [("Yes", "yes"), ("No", "no")]


def test_activity_and_passive_consents_recommend_yes_but_safety_stays_no_first():
    for qid in ("c_act", "c_pas"):
        assert [(o.label, o.value) for o in _q(qid).options] == [
            ("Yes (recommended)", "yes"),
            ("No", "no"),
        ]
    for qid in ("safe1", "safe2"):
        assert [(o.label, o.value) for o in _q(qid).options] == [("No", "no"), ("Yes", "yes")]


def test_excluded_questions_are_absent():
    ids = {q.id for q in build_questions()}
    for banned in (
        "reply_pace",
        "outside_support",
        "recurring_messages",
        "after_no_reply",
        "flavor",
        "gemini",
        # derived from `armed`: Gumi writes first whenever scheduled messages are allowed
        "first",
    ):
        assert banned not in ids
