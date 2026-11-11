"""Pure onboarding state machine (no Telegram, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from relic.onboarding.battery_text import BATTERY_PRESENTATION
from relic.onboarding.questions import Question

SKIP = "__skip__"


@dataclass(frozen=True)
class AnswerResult:
    ok: bool
    error: str = ""


class Engine:
    def __init__(
        self,
        questions: Sequence[Question],
        answers: Mapping[str, str],
        catalog: str | None = BATTERY_PRESENTATION,
    ):
        """``catalog`` is the battery version the saved ``answers`` were collected with.

        Callers loading saved progress pass ``progress.get("catalog")``: a missing or
        different version means the ``b*`` ids may name other questions now.
        """
        self.questions = list(questions)
        known = {q.id for q in self.questions}
        self.answers: dict[str, str] = {k: v for k, v in answers.items() if k in known}
        if len(self.answers) != len(answers) or catalog != BATTERY_PRESENTATION:
            # Progress from an older catalog (for example the removed "welcome" or
            # b44..b48, or a telegram-v1 save): ignore unknown ids. The battery ids it
            # reused (b01..b43) meant different questions, so those answers go too
            # rather than being reinterpreted.
            battery = {q.id for q in self.questions if q.in_battery}
            self.answers = {k: v for k, v in self.answers.items() if k not in battery}

    def remaining(self) -> list[Question]:
        return [
            q for q in self.questions if q.id not in self.answers and q.when(self.answers)
        ]

    def current(self) -> Question | None:
        rest = self.remaining()
        return rest[0] if rest else None

    def by_id(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.id == qid), None)

    def answer(self, qid: str, raw: str) -> AnswerResult:
        question = self.by_id(qid)
        current = self.current()
        if question is None or current is None or current.id != qid:
            return AnswerResult(False, "stale")
        if question.kind == "choice":
            if raw not in {option.value for option in question.options}:
                return AnswerResult(False, "Please use the buttons.")
            self.answers[qid] = raw
            return AnswerResult(True)
        if raw == SKIP and question.skip_label:
            self.answers[qid] = SKIP
            return AnswerResult(True)
        value = question.validate(raw) if question.validate else raw.strip()
        if not value:
            return AnswerResult(
                False, question.invalid_message or "That doesn't look right. Try again."
            )
        self.answers[qid] = value
        return AnswerResult(True)

    def progress_in_battery(self) -> tuple[int, int]:
        battery = [q for q in self.questions if q.in_battery]
        return sum(1 for q in battery if q.id in self.answers), len(battery)

    def restart(self) -> None:
        self.answers.clear()
