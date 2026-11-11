from __future__ import annotations

import pytest

from relic.gumi_plugin.output_sanitizer import (
    sanitize_for_subject,
    strip_terminal_full_stops,
)


def test_strip_terminal_full_stops_preserves_empty_text():
    assert strip_terminal_full_stops("") == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("Caption.", "Caption"),
        ("Caption...", "Caption"),
    ),
)
def test_strip_terminal_full_stops_removes_one_or_more_full_stops(text, expected):
    assert strip_terminal_full_stops(text) == expected


def test_strip_terminal_full_stops_preserves_internal_punctuation():
    assert strip_terminal_full_stops("Wait. Is this ready?") == "Wait. Is this ready?"


def test_repair_obvious_question_tail_adds_missing_question_mark():
    text = "C'è una differenza tra impulso e fretta, no"

    assert sanitize_for_subject(text) == text + "?"


def test_repair_obvious_question_tail_leaves_other_endings_unchanged():
    assert sanitize_for_subject("È una cosa bella") == "È una cosa bella"


def test_rejects_traceback_header_without_leaking_payload(capsys):
    text = 'Traceback (most recent call last):\n  File "/srv/private/worker.py", line 8, in run\nRuntimeError: token=secret'

    assert sanitize_for_subject(text) is None
    assert capsys.readouterr().err == ""


def test_rejects_standalone_traceback_frame_without_leaking_payload(capsys):
    text = 'Retrying request.\n  File "/srv/private/worker.py", line 8, in run\nRuntimeError: token=secret'

    assert sanitize_for_subject(text) is None
    assert capsys.readouterr().err == ""


def test_removes_operator_line_without_writing_it_to_stderr(capsys):
    text = "Visible text.\n[WARN] token=secret\nStill visible."

    assert sanitize_for_subject(text) == "Visible text.\nStill visible."
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "text",
    (
        "echo deploy.",
        "```sh\necho deploy.\n```",
        "Path: /srv/releases/current.",
        "Waiting...",
    ),
)
def test_preserves_terminal_punctuation_in_technical_content(text):
    assert sanitize_for_subject(text) == text


def test_preserves_safe_text_exactly():
    text = "Safe technical text.\nUse `python -m app`."

    assert sanitize_for_subject(text) == text
