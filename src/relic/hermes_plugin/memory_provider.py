"""Hermes memory provider for User-scoped continuity."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relic.shared_continuity.runtime_store import RuntimeContinuityStore


logger = logging.getLogger(__name__)
_REDACTED = "[redacted]"
_SHA256_CAP = 65536


def _sha256(text: str) -> str:
    return hashlib.sha256(text[:_SHA256_CAP].encode()).hexdigest()


class RelicMemoryProvider:
    """Surface confirmed continuity only within one User runtime scope."""

    def __init__(
        self,
        *,
        runtime_scope_id: str,
        hermes_profile_id: str | None = None,
        max_prefetch: int = 5,
        relic_home: str | None = None,
    ) -> None:
        if not isinstance(runtime_scope_id, str) or not runtime_scope_id.strip():
            raise ValueError("RelicMemoryProvider requires a non-empty runtime_scope_id")
        self._runtime_scope_id = runtime_scope_id
        self._hermes_profile_id = hermes_profile_id or ""
        self._max_prefetch = max_prefetch
        self._relic_home = relic_home or os.environ.get("RELIC_HOME") or None
        self._store: RuntimeContinuityStore | None = None

    def _get_store(self) -> RuntimeContinuityStore:
        if self._store is None:
            self._store = RuntimeContinuityStore(self._relic_home)
        return self._store

    @staticmethod
    def _is_safe_to_surface(marker: dict[str, Any]) -> bool:
        return marker.get("user_confirmation") is True

    @staticmethod
    def _format_marker(marker: dict[str, Any]) -> str:
        words = marker.get("user_words") or []
        return " ".join(str(word) for word in words).strip() if isinstance(words, list) else ""

    def prefetch(self, query: str) -> str:
        """Return confirmed User continuity as compact context text."""
        try:
            markers = self._get_store().get_recent_markers(
                runtime_scope_id=self._runtime_scope_id,
                hermes_profile_id=self._hermes_profile_id or None,
                limit=self._max_prefetch,
            )
            return "\n".join(
                line
                for marker in markers
                if self._is_safe_to_surface(marker)
                if (line := self._format_marker(marker))
            )
        except Exception:
            logger.exception("RelicMemoryProvider.prefetch failed, returning empty")
            return ""

    def pending_candidates(self) -> list[dict[str, str]]:
        """Return scoped, unconfirmed proposals for explicit user review.

        These are deliberately separate from ``prefetch``: pending proposals
        are not recalled continuity and must be presented as questions.
        """
        try:
            markers = self._get_store().get_recent_candidates(
                runtime_scope_id=self._runtime_scope_id,
                hermes_profile_id=self._hermes_profile_id or None,
                limit=min(max(self._max_prefetch, 1), 5),
            )
            candidates: list[dict[str, str]] = []
            for marker in markers:
                marker_id = marker.get("marker_id")
                wording = self._format_marker(marker)
                if isinstance(marker_id, str) and marker_id and wording:
                    candidates.append({"marker_id": marker_id, "wording": wording})
            return candidates
        except Exception:
            logger.exception("RelicMemoryProvider.pending_candidates failed, returning empty")
            return []

    def confirm_candidate(self, marker_id: str) -> dict[str, Any] | None:
        """Confirm a candidate only in this provider's runtime scope."""
        try:
            return self._get_store().confirm_candidate(
                marker_id=marker_id,
                runtime_scope_id=self._runtime_scope_id,
                hermes_profile_id=self._hermes_profile_id or None,
            )
        except Exception:
            logger.exception("RelicMemoryProvider.confirm_candidate failed, ignoring")
            return None

    def reject_candidate(self, marker_id: str) -> dict[str, Any] | None:
        """Reject a candidate only in this provider's runtime scope."""
        try:
            return self._get_store().reject_candidate(
                marker_id=marker_id,
                runtime_scope_id=self._runtime_scope_id,
                hermes_profile_id=self._hermes_profile_id or None,
            )
        except Exception:
            logger.exception("RelicMemoryProvider.reject_candidate failed, ignoring")
            return None


    def sync_turn(self, user_msg: str, assistant_msg: str) -> None:
        """Record turn identity and capture a consented pending check-in reply.

        Raw text is written only to the private singleton check-in database
        when its explicit active-elicitation consent is enabled; Hermes
        continuity storage still receives hashes only.
        """
        try:
            try:
                from relic.checkin.reply_capture import capture_reply_if_pending

                checkin_dir = (
                    Path(self._relic_home or os.environ.get("RELIC_HOME") or Path.home() / ".relic")
                    / "user"
                    / "checkin"
                )
                capture_reply_if_pending(
                    user_msg,
                    self._runtime_scope_id,
                    self._relic_home,
                    db_path=checkin_dir / "relic.db",
                    delivery_policy_path=checkin_dir / "delivery_policy.json",
                )
            except Exception:
                logger.debug("RelicMemoryProvider check-in capture failed", exc_info=True)
            logger.debug(
                "sync_turn runtime_scope=%s user_hash=%s assistant_hash=%s ts=%s",
                self._runtime_scope_id,
                _sha256(user_msg)[:12],
                _sha256(assistant_msg)[:12],
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            logger.exception("RelicMemoryProvider.sync_turn failed, ignoring")
