"""Per-topic cooldown for delivered check-in and proactive messages.

Once Gumi delivers a message about a topic, that topic is paused until a
random moment 2-7 days later. Without it, each hourly run saw the same latest
subject thread as its only anchor and reopened it again (a repeated-topic case:
five delivered messages on one topic in five days).

The store is a JSON list next to the check-in database:
    [{"topic": "modellino", "delivered_at": iso, "paused_until": iso}, ...]
All readers are fail-open: an unreadable store means no paused topics.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAUSE_MIN_DAYS = 2
PAUSE_MAX_DAYS = 7
STORE_NAME = "topic_pauses.json"

# Topic words shorter than this, or in the stoplist, never match on their own:
# a topic like "il trasloco" must pause "trasloco", not every "il".
_MIN_WORD_LEN = 4
_STOPWORDS = frozenset({
    "alla", "allo", "anche", "come", "con", "dalla", "dello", "della", "delle",
    "degli", "dopo", "fatto", "fare", "nella", "nello", "nelle", "negli",
    "oggi", "ieri", "prima", "quel", "quella", "quello", "sulla", "sullo",
    "tuo", "tua", "tuoi", "tue", "cosa", "cose",
})
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def store_path(store_dir: Path) -> Path:
    return Path(store_dir) / STORE_NAME


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def normalize_topic(topic: str) -> str:
    return " ".join(_WORD_RE.findall(topic.lower()))[:60]


def record_topic_pause(
    store_dir: Path,
    topic: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime | None:
    """Pause ``topic`` until a random moment PAUSE_MIN_DAYS..PAUSE_MAX_DAYS ahead."""
    key = normalize_topic(topic or "")
    if not key:
        return None
    current = _now(now)
    delay = (rng or random).uniform(PAUSE_MIN_DAYS * 86400, PAUSE_MAX_DAYS * 86400)
    until = current + timedelta(seconds=delay)
    path = store_path(store_dir)
    rows = [
        row for row in _load(path)
        if row.get("topic") != key and _parse(row.get("paused_until")) > current
    ]
    rows.append({
        "topic": key,
        "delivered_at": current.isoformat(),
        "paused_until": until.isoformat(),
    })
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as exc:
        print(f"[topic_pause] write failed: {exc}", file=sys.stderr)
        return None
    return until


def _parse(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def active_topics(store_dir: Path, *, now: datetime | None = None) -> list[str]:
    current = _now(now)
    return [
        str(row["topic"]) for row in _load(store_path(store_dir))
        if row.get("topic") and _parse(row.get("paused_until")) > current
    ]


def mentions_topic(text: str, topics: list[str]) -> bool:
    """True when ``text`` contains a significant word of any paused topic."""
    if not text or not topics:
        return False
    words = set(_WORD_RE.findall(text.lower()))
    return any(
        word in words
        for topic in topics
        for word in topic.split()
        if len(word) >= _MIN_WORD_LEN and word not in _STOPWORDS
    )


def render_paused_section(topics: list[str]) -> str:
    if not topics:
        return ""
    return (
        "\n--- argomenti in pausa (ne hai già parlato di recente: non riprenderli, "
        "nemmeno indirettamente) ---\n" + "\n".join(f"• {t}" for t in topics)
    )
