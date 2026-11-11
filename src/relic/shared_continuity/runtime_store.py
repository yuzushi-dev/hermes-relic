"""Canonical User-scoped continuity persistence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relic.paths import get_relic_home


class RuntimeContinuityStore:
    """Small durable seam for continuity recalled by the shipping hooks."""

    def __init__(self, relic_home: str | Path | None = None) -> None:
        self._user_home = Path(relic_home) / "user" if relic_home else get_relic_home() / "user"
        self._path = self._user_home / "continuity.json"

    def get_recent_markers(
        self,
        *,
        runtime_scope_id: str,
        hermes_profile_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        markers = self._load().get("markers", [])
        selected = [
            marker
            for marker in markers
            if marker.get("runtime_scope_id") == runtime_scope_id
            and marker.get("user_confirmation") is True
            and (
                not hermes_profile_id
                or marker.get("hermes_profile_id") == hermes_profile_id
            )
        ]
        selected.sort(key=lambda marker: marker.get("created_at", ""), reverse=True)
        return selected[:limit]

    def get_recent_candidates(
        self,
        *,
        runtime_scope_id: str,
        hermes_profile_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        candidates = [
            marker
            for marker in self._load().get("markers", [])
            if marker.get("runtime_scope_id") == runtime_scope_id
            and marker.get("candidate_for_confirmation") is True
            and (
                not hermes_profile_id
                or marker.get("hermes_profile_id") == hermes_profile_id
            )
        ]
        candidates.sort(key=lambda marker: marker.get("created_at", ""), reverse=True)
        return candidates[:limit]

    def propose_candidate(
        self,
        *,
        runtime_scope_id: str,
        user_words: list[str],
        source_type: str = "hindsight",
        hermes_profile_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._load()
        marker = {
            "marker_id": f"candidate-{os.urandom(8).hex()}",
            "runtime_scope_id": runtime_scope_id,
            "hermes_profile_id": hermes_profile_id or "",
            "user_confirmation": False,
            "candidate_for_confirmation": True,
            "source_type": source_type,
            "user_words": [word for word in user_words if word],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["markers"].append(marker)
        self._save(state)
        return marker
    def confirm_candidate(
        self,
        *,
        marker_id: str,
        runtime_scope_id: str,
        hermes_profile_id: str | None = None,
        user_words: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Promote one explicitly confirmed candidate within its own scope."""
        state = self._load()
        for marker in state["markers"]:
            if (
                marker.get("marker_id") != marker_id
                or marker.get("runtime_scope_id") != runtime_scope_id
                or marker.get("candidate_for_confirmation") is not True
                or (
                    hermes_profile_id
                    and marker.get("hermes_profile_id") != hermes_profile_id
                )
            ):
                continue
            marker["user_confirmation"] = True
            marker["candidate_for_confirmation"] = False
            marker["source_type"] = "subject_confirmed"
            marker["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            if user_words is not None:
                marker["user_words"] = [word for word in user_words if word]
            self._save(state)
            return dict(marker)
        return None

    def reject_candidate(
        self,
        *,
        marker_id: str,
        runtime_scope_id: str,
        hermes_profile_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Remove one explicitly rejected candidate within its own scope."""
        state = self._load()
        for index, marker in enumerate(state["markers"]):
            if (
                marker.get("marker_id") != marker_id
                or marker.get("runtime_scope_id") != runtime_scope_id
                or marker.get("candidate_for_confirmation") is not True
                or (
                    hermes_profile_id
                    and marker.get("hermes_profile_id") != hermes_profile_id
                )
            ):
                continue
            rejected = state["markers"].pop(index)
            self._save(state)
            return rejected
        return None



    def remember(
        self,
        *,
        runtime_scope_id: str,
        user_words: list[str],
        hermes_profile_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._load()
        marker = {
            "runtime_scope_id": runtime_scope_id,
            "hermes_profile_id": hermes_profile_id or "",
            "user_confirmation": True,
            "user_words": [word for word in user_words if word],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["markers"].append(marker)
        self._save(state)
        return marker

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self._path.exists():
            return {"markers": []}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"markers": []}
        markers = value.get("markers") if isinstance(value, dict) else None
        return {"markers": markers if isinstance(markers, list) else []}

    def _save(self, state: dict[str, list[dict[str, Any]]]) -> None:
        self._user_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".continuity.", dir=self._user_home)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(state, sort_keys=True, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
