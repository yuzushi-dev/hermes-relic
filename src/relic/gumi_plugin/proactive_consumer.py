"""Proactive queue consumer (Plan §Task 9).

Pulls one candidate from the canonical User queue when a runtime scope is
active, otherwise from the legacy per-subject queue. Expired or low-salience
candidates are skipped; consumed entries are rewritten out so the queue
shrinks deterministically. Does not deliver, returns the tuple so the
downstream dispatch path is the same as the legacy proactivity lane.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relic.hermes_runtime import RuntimeDecision, RuntimeDecisionReason

logger = logging.getLogger(__name__)


def _configured_relic_home(relic_home: Optional[Path] = None) -> Path:
    return Path(relic_home or os.environ.get("RELIC_HOME") or Path.home() / ".relic")


def proactive_queue_path(
    subject_id: str,
    relic_home: Optional[Path] = None,
    *,
    canonical: bool = False,
) -> Path:
    home = _configured_relic_home(relic_home)
    use_canonical = canonical or os.environ.get("RELIC_RUNTIME_SCOPE_ID") == subject_id
    if not use_canonical:
        profile_path = home / "user" / "user_profile.json"
        if not profile_path.is_symlink() and profile_path.is_file():
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                profile = None
            use_canonical = (
                isinstance(profile, dict)
                and profile.get("runtime_scope_id") == subject_id
            )
    if use_canonical:
        return home / "user" / "checkin" / "proactive_queue.jsonl"
    return home / "subjects" / subject_id / "proactive_queue.jsonl"


def _validate_queue_path(
    path: Path, *, trusted_home: Optional[Path] = None
) -> Path:
    path = Path(path)
    stop_at = Path(trusted_home) if trusted_home is not None else None
    if stop_at is not None:
        try:
            relative_path = path.relative_to(stop_at)
        except ValueError as exc:
            raise ValueError("proactive queue is invalid") from exc
        if ".." in relative_path.parts:
            raise ValueError("proactive queue is invalid")

    current = path
    while True:
        if current == stop_at:
            break
        if current.is_symlink():
            raise ValueError("proactive queue is invalid")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return path


def _queue_path(subject_id: str, relic_home: Optional[Path] = None) -> Path:
    return proactive_queue_path(subject_id, relic_home)


def load_candidates(
    path: Path, *, trusted_home: Optional[Path] = None
) -> list[dict]:
    path = _validate_queue_path(Path(path), trusted_home=trusted_home)
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def save_candidates(
    path: Path, candidates: list[dict], *, trusted_home: Optional[Path] = None
) -> None:
    path = _validate_queue_path(Path(path), trusted_home=trusted_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_queue_path(path, trusted_home=trusted_home)
    tmp = path.with_suffix(path.suffix + ".tmp")
    _validate_queue_path(tmp, trusted_home=trusted_home)
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in candidates:
            fh.write(json.dumps(row) + "\n")
    tmp.replace(path)
    os.chmod(path, 0o600)


def _to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def consume_one(
    subject_id: str,
    hermes_home: Path,
    relic_home: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
    min_priority: float = 0.5,
    queue_path: Optional[Path] = None,
    trusted_home: Optional[Path] = None,
) -> tuple[RuntimeDecision, list[RuntimeDecisionReason], Optional[dict]]:
    """Pop the highest-priority unexpired candidate and convert it to a decision tuple.

    Behaviour:
      - empty queue or no eligible candidate → (NO_REPLY, [no_due_work], None);
      - candidate accepted → (DELIVER, [no_due_work], {"message": ..., ...});
      - low priority candidates are dropped from the queue silently.
    """
    now = now or datetime.now(timezone.utc)
    path = Path(queue_path) if queue_path is not None else _queue_path(subject_id, relic_home)
    if trusted_home is None and queue_path is None:
        trusted_home = _configured_relic_home(relic_home)
    elif trusted_home is None and relic_home is not None:
        trusted_home = relic_home
    candidates = load_candidates(path, trusted_home=trusted_home)
    if not candidates:
        return RuntimeDecision.NO_REPLY, [RuntimeDecisionReason.no_due_work], None

    remaining: list[dict] = []
    chosen: Optional[dict] = None
    for row in candidates:
        if row.get("consumed_at"):
            remaining.append(row)
            continue
        expires_at = _to_dt(row.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            # expired: drop silently.
            continue
        priority = float(row.get("priority", 0.0) or 0.0)
        if priority < min_priority:
            # below floor: drop.
            continue
        if chosen is None or float(row.get("priority", 0.0)) > float(chosen.get("priority", 0.0)):
            if chosen is not None:
                remaining.append(chosen)
            chosen = row
        else:
            remaining.append(row)

    if chosen is None:
        save_candidates(path, remaining, trusted_home=trusted_home)
        return RuntimeDecision.NO_REPLY, [RuntimeDecisionReason.no_due_work], None

    chosen["consumed_at"] = now.isoformat()
    remaining.append(chosen)
    save_candidates(path, remaining, trusted_home=trusted_home)

    base_message = "DELIVER\ntipo: text\nora: -\nproactive: true"
    candidate_data = {
        "message": base_message,
        "decision_type": "proactivity",
        "proactive_signal_ref": chosen.get("signal_ref"),
        "suggested_posture": chosen.get("suggested_posture"),
        "priority": chosen.get("priority"),
        "candidate_id": chosen.get("id"),
    }
    return RuntimeDecision.DELIVER, [RuntimeDecisionReason.no_due_work], candidate_data


def _proactive_queue_enabled() -> bool:
    return os.environ.get("RELIC_PROACTIVE_QUEUE_ENABLED", "").strip().lower() in ("1", "true", "yes")
