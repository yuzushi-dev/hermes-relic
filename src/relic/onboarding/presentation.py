"""Question presentation shared by the onboarding channels (Telegram, Desktop).

The texts come verbatim from the catalog (``questions.py`` / ``battery_text.py``);
this module only decides how they are shown: battery counter, preamble and the
fixed always-off summary rules. No channel I/O lives here.
"""

from __future__ import annotations

from typing import Mapping

from relic.onboarding.engine import SKIP, Engine
from relic.onboarding.questions import Question, build_questions

SUMMARY_PROMPT = "__summary__"


def question_text(question: Question, engine: Engine) -> str:
    """Preamble + "(done+1/total)" counter + prompt; the summary replaces its prompt."""
    prompt = (
        summary(engine.answers)
        if question.prompt == SUMMARY_PROMPT
        else question.prompt
    )
    parts = []
    if question.preamble:
        parts.append(question.preamble)
    if question.in_battery:
        done, total = engine.progress_in_battery()
        prompt = f"({done + 1}/{total}) {prompt}"
    parts.append(prompt)
    return "\n\n".join(parts)


def summary(answers: Mapping[str, str]) -> str:
    """The fixed closing rules and the always-off lines every channel must show."""

    def label(qid: str) -> str:
        question = next((q for q in build_questions() if q.id == qid), None)
        value = answers.get(qid)
        if question is None or value is None:
            return "-"
        if value == SKIP:
            return "skipped"
        for option in question.options:
            if option.value == value:
                return option.label
        return value

    lines = [
        "Here is your setup:",
        f"- Time zone: {answers.get('tz', '-')}",
        f"- Name: {label('name')}",
        f"- Setup: {label('mode')}",
        "- Baseline questions: answered",
    ]
    for qid, title in (
        ("msglen", "Message length"),
        ("emoji", "Emoji"),
        ("tone", "Tone"),
        ("cont", "Memory"),
        ("img", "Images"),
        ("aud", "Audio"),
        ("mus", "Music"),
        ("armed", "Messages from me"),
        ("freq", "Frequency"),
        ("quiet", "Quiet hours"),
        ("c_act", "Questions from me"),
        ("c_pas", "Learning from chats"),
    ):
        if qid in answers:
            lines.append(f"- {title}: {label(qid)}")
    lines.append(
        "\nThis setup does NOT include:\n"
        "- No person reviews this chat; there is no human behind me.\n"
        "- I have no emergency contacts: in an emergency call 112 or your local "
        "emergency services.\n"
        "Exclusivity language and guilt-tripping after you don't reply are "
        "always off.\n"
        "You can redo this setup anytime with /gumi-setup."
    )
    return "\n".join(lines)
