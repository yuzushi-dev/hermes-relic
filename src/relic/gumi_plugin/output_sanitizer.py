"""Output sanitizer, single point of control before User-facing stdout.

`sanitize_for_user(text)` is called before every print() that reaches
the User's chat. Returns None to silently drop the message.

Deny patterns match operator-facing markers that must never appear in chat:
[WARN], [DRY-RUN], [SILENT], MEDIA:, Traceback, ERROR:, *Error:, *Exception:
"""
from __future__ import annotations

import re

_TRACEBACK_HEADER_RE = re.compile(
    r"^\s*Traceback\s*\(most recent call last\)\s*:?\s*$", re.IGNORECASE
)
_TRACEBACK_FRAME_RE = re.compile(
    r'^\s*File "[^"\n]+", line \d+(?:, in .+)?\s*$'
)

# Lines starting with these prefixes are operator telemetry, never User-facing.
_DENY_LINE_RE = re.compile(
    r"^\s*("
    r"\[WARN\]"
    r"|\[DRY-RUN\]"
    r"|\[SILENT\]"
    r"|MEDIA:"
    r"|Traceback\s*\(most recent call last\)"
    r"|ERROR:"
    r"|[A-Za-z]+Error:"
    r"|[A-Za-z]+Exception:"
    r")",
    re.IGNORECASE,
)
_OBVIOUS_QUESTION_TAIL_RE = re.compile(r",\s*(?:no|vero|giusto|eh)$", re.IGNORECASE)


def strip_terminal_full_stops(text: str) -> str:
    """Remove trailing full stops while preserving other punctuation."""
    return text.rstrip(".")


def repair_obvious_question_tail(text: str) -> str:
    """Restore a missing question mark after a colloquial question tag."""
    stripped = text.rstrip()
    if _OBVIOUS_QUESTION_TAIL_RE.search(stripped):
        return stripped + "?"
    return text


def sanitize_for_user(text: str) -> str | None:
    """Remove operator telemetry; reject Python tracebacks."""
    if not text or not text.strip():
        return None

    if any(
        _TRACEBACK_HEADER_RE.match(line) or _TRACEBACK_FRAME_RE.match(line)
        for line in text.splitlines()
    ):
        return None

    result = "".join(
        line
        for line in text.splitlines(keepends=True)
        if not _DENY_LINE_RE.match(line)
    ).strip()
    return repair_obvious_question_tail(result) or None

# Private compatibility for unconverted delivery modules.
sanitize_for_subject = sanitize_for_user
