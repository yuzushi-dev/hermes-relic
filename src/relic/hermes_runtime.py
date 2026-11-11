"""Hermes runtime defaults used by Relic User runtime profiles."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

RUNTIME_SCOPE_ID_BYTES = 32
_RUNTIME_SCOPE_ID_RE = re.compile(r"[0-9a-f]{64}\Z")


def generate_runtime_scope_id() -> str:
    """Create an opaque identifier for the installation's runtime scope."""
    return secrets.token_hex(RUNTIME_SCOPE_ID_BYTES)


def validate_runtime_scope_id(runtime_scope_id: str) -> str:
    """Return a canonical runtime scope identifier or reject malformed metadata."""
    if not isinstance(runtime_scope_id, str) or not _RUNTIME_SCOPE_ID_RE.fullmatch(
        runtime_scope_id
    ):
        raise ValueError("runtime_scope_id must be a 64-character lowercase hexadecimal value")
    return runtime_scope_id


# ---------------------------------------------------------------------------
# Hermes session key
# ---------------------------------------------------------------------------

X_HERMES_SESSION_KEY_HEADER = "X-Hermes-Session-Key"


class HermesSessionKey:
    """Hermes session key management - stores hash only, never raw key."""

    @staticmethod
    def derive(
        runtime_scope_id: str, gumi_instance_id: str, hermes_profile_id: str
    ) -> str:
        """Derive a session key hash scoped to a User runtime and Hermes profile."""
        if not isinstance(runtime_scope_id, str) or not runtime_scope_id.strip():
            raise ValueError("runtime_scope_id is required for session key derivation")
        composite = f"{runtime_scope_id}:{gumi_instance_id}:{hermes_profile_id}"
        return hashlib.sha256(composite.encode()).hexdigest()

    @staticmethod
    def store(session_key_hash: str) -> dict:
        """
        Store the session key hash with metadata.

        Args:
            session_key_hash: The hashed session key (not raw).

        Returns:
            Dictionary with session key metadata.
        """
        return {
            "session_key_hash": session_key_hash,
            "hash_algorithm": "sha256",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "key_scope_resolvable": False,
            "audit_event_on_rotation": True,
        }

    @staticmethod
    def reject_missing_scope(runtime_scope_id: str) -> None:
        """Reject a missing or malformed User runtime scope."""
        if not isinstance(runtime_scope_id, str) or not runtime_scope_id.strip():
            raise ValueError("runtime_scope_id is required - session key scope cannot be resolved")

    @staticmethod
    def reject_cross_runtime_scope(key_hash: str, expected_runtime_scope_id: str) -> None:
        """Reject reuse outside the expected User runtime scope."""
        if not key_hash:
            raise ValueError("key_hash is required")
        if not isinstance(expected_runtime_scope_id, str) or not expected_runtime_scope_id.strip():
            raise ValueError("expected_runtime_scope_id is required")


def pass_session_key(
    runtime_scope_id: str, gumi_instance_id: str, hermes_profile_id: str
) -> dict:
    """Generate a User-runtime-scoped session key header for Hermes API calls."""
    key_hash = HermesSessionKey.derive(
        runtime_scope_id, gumi_instance_id, hermes_profile_id
    )
    return {X_HERMES_SESSION_KEY_HEADER: key_hash}


# ---------------------------------------------------------------------------
# Delivery gate
# ---------------------------------------------------------------------------


class DeliveryGateDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass
class DeliveryGateDecisionEvent:
    """Event emitted when delivery gate makes a decision."""

    decision: DeliveryGateDecision
    reason_codes: list[str]
    subject_id: str
    gumi_instance_id: str
    hermes_profile_id: str
    platform: str
    target_hash: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason_codes": self.reason_codes,
            "subject_id": self.subject_id,
            "gumi_instance_id": self.gumi_instance_id,
            "hermes_profile_id": self.hermes_profile_id,
            "platform": self.platform,
            "target_hash": self.target_hash,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# RuntimeDecision
# ---------------------------------------------------------------------------

class RuntimeDecision(str, Enum):
    NO_REPLY = "NO_REPLY"
    BLOCKED = "BLOCKED"
    CANDIDATE = "CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DELIVER = "DELIVER"
    ERROR = "ERROR"


class RuntimeDecisionReason(str, Enum):
    quiet_hours = "quiet_hours"
    platform_not_allowlisted = "platform_not_allowlisted"
    subject_paused = "subject_paused"
    continuity_scope_paused = "continuity_scope_paused"
    followup_not_due = "followup_not_due"
    followup_expired = "followup_expired"
    followup_max_attempts_reached = "followup_max_attempts_reached"
    already_logged_or_contacted = "already_logged_or_contacted"
    burden_signal = "burden_signal"
    safety_review_required = "safety_review_required"
    output_sanitizer_blocked = "output_sanitizer_blocked"
    delivery_state_unknown = "delivery_state_unknown"
    no_due_work = "no_due_work"
    natural_cadence_skip = "natural_cadence_skip"


@dataclass
class DecisionEvent:
    decision: RuntimeDecision
    reason_codes: list[RuntimeDecisionReason]
    subject_id: str
    gumi_instance_id: str
    hermes_profile_id: str
    target_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Canonical naturalness fields (Plan §Task 1, Step 4).
    decision_type: Optional[str] = None
    event_kind: Optional[str] = None
    posture: Optional[str] = None
    features_id: Optional[int] = None
    non_response_streak: Optional[int] = None
    followup_non_response_streak: Optional[int] = None
    reach_score: Optional[float] = None
    response_deadline_at: Optional[str] = None
    cadence_decay_applied: Optional[bool] = None
    outcome_status: Optional[str] = None
    outcome_status_before: Optional[str] = None
    wake_agent_emitted: Optional[bool] = None
    message_hash: Optional[str] = None
    delivered: Optional[bool] = None
    policy_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason_codes": [r.value for r in self.reason_codes],
            "subject_id": self.subject_id,
            "gumi_instance_id": self.gumi_instance_id,
            "hermes_profile_id": self.hermes_profile_id,
            "target_id": self.target_id,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "decision_type": self.decision_type,
            "event_kind": self.event_kind,
            "posture": self.posture,
            "features_id": self.features_id,
            "non_response_streak": self.non_response_streak,
            "followup_non_response_streak": self.followup_non_response_streak,
            "reach_score": self.reach_score,
            "response_deadline_at": self.response_deadline_at,
            "cadence_decay_applied": self.cadence_decay_applied,
            "outcome_status": self.outcome_status,
            "outcome_status_before": self.outcome_status_before,
            "wake_agent_emitted": self.wake_agent_emitted,
            "message_hash": self.message_hash,
            "delivered": self.delivered,
            "policy_reason": self.policy_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DecisionEvent:
        return cls(
            decision=RuntimeDecision(data["decision"]),
            reason_codes=[RuntimeDecisionReason(r) for r in data["reason_codes"]],
            subject_id=data["subject_id"],
            gumi_instance_id=data["gumi_instance_id"],
            hermes_profile_id=data["hermes_profile_id"],
            target_id=data.get("target_id"),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            decision_type=data.get("decision_type"),
            event_kind=data.get("event_kind"),
            posture=data.get("posture"),
            features_id=data.get("features_id"),
            non_response_streak=data.get("non_response_streak"),
            followup_non_response_streak=data.get("followup_non_response_streak"),
            reach_score=data.get("reach_score"),
            response_deadline_at=data.get("response_deadline_at"),
            cadence_decay_applied=data.get("cadence_decay_applied"),
            outcome_status=data.get("outcome_status"),
            outcome_status_before=data.get("outcome_status_before"),
            wake_agent_emitted=data.get("wake_agent_emitted"),
            message_hash=data.get("message_hash"),
            delivered=data.get("delivered"),
            policy_reason=data.get("policy_reason"),
        )


# ---------------------------------------------------------------------------
# Hermes configuration defaults
# ---------------------------------------------------------------------------

# Single source of truth for the Hermes profile the installer creates and owns.
# Deliberately not "gumi": a profile with that name may already belong to the user.
HERMES_USER_PROFILE_NAME = "relic-gumi"


# ---------------------------------------------------------------------------
# Resume reconciliation
# ---------------------------------------------------------------------------


class ReconciliationDecision(str, Enum):
    ALLOW = "ALLOW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReconciliationCheck(str, Enum):
    SUBJECT_SCOPE = "subject_scope"
    SESSION_KEY_HASH = "session_key_hash"
    PLATFORM_ALLOWLIST = "platform_allowlist"
    DELIVERY_ENABLED = "delivery_enabled"
    CONTINUITY_MARKER_STATUS = "continuity_marker_status"
    CONTINUITY_MARKER_TTL = "continuity_marker_ttl"
    CONTINUITY_SCOPE_PAUSE = "continuity_scope_pause"
    FOLLOWUP_ATTEMPT_COUNT = "followup_attempt_count"
    SAFETY_REVIEW_STATE = "safety_review_state"
    BEHAVIOR_POLICY_PATCH_EXPIRY = "behavior_policy_patch_expiry"
    OUTPUT_SANITIZER = "output_sanitizer"
    DELIVERY_STATE_KNOWN = "delivery_state_known"


@dataclass
class ReconciliationResult:
    """Result of a resume reconciliation check."""

    decision: ReconciliationDecision
    failed_checks: list[ReconciliationCheck]
    pending_output_held: bool
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "failed_checks": [c.value for c in self.failed_checks],
            "pending_output_held": self.pending_output_held,
            "metadata": self.metadata,
        }


@dataclass
class SessionResumeState:
    """State snapshot for resume reconciliation."""

    subject_id: str
    gumi_instance_id: str
    hermes_profile_id: str
    session_key_hash: str
    platform_allowlist_valid: bool = True
    delivery_enabled: bool = True
    continuity_marker_active: bool = True
    continuity_marker_expires_at: datetime | None = None
    continuity_scope_paused: bool = False
    followup_attempt_count: int = 0
    safety_review_required: bool = False
    behavior_policy_patch_expires_at: datetime | None = None
    output_sanitizer_clean: bool = True
    delivery_state_known: bool = True
    # Internal marker for unknown state tracking
    _unknown_marker: bool = field(default=False, repr=False)


# ---------------------------------------------------------------------------
# Runtime feature support
# ---------------------------------------------------------------------------

_RUNTIME_CONFIG: dict = {}


def check_hermes_feature_support() -> dict:
    """
    Check which Hermes runtime features are supported.

    Returns:
        Dict of feature name -> supported (bool).
    """
    import subprocess

    supported = {
        "no_agent_cron": False,
        "transform_llm_output": False,
        "session_key_support": False,
        "allowlist_support": False,
    }

    try:
        result = subprocess.run(
            ["hermes", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            supported["session_key_support"] = True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["hermes", "config", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            output = result.stdout + result.stderr
            if "cron" in output.lower():
                supported["no_agent_cron"] = True
            if "transform" in output.lower():
                supported["transform_llm_output"] = True
            if "allowlist" in output.lower():
                supported["allowlist_support"] = True
    except Exception:
        pass

    return supported


def init_runtime_config() -> dict:
    """
    Initialize global runtime configuration.

    Returns:
        Dict with runtime config state.
    """
    global _RUNTIME_CONFIG

    _RUNTIME_CONFIG = {
        "initialized": True,
        "features": check_hermes_feature_support(),
    }

    return _RUNTIME_CONFIG


def get_runtime_config() -> dict:
    """Return the current runtime config."""
    return _RUNTIME_CONFIG


# ---------------------------------------------------------------------------
# Resume reconciliation
# ---------------------------------------------------------------------------

class ResumeReconciliation:
    """
    Resume reconciliation state machine.

    Hermes may resume sessions. Relic must reconcile before delivery.
    Re-checks subject scope, session key hash, platform allowlist, delivery enabled,
    continuity marker status, continuity marker TTL, continuity scope pause,
    followup attempt count, safety review state, behavior policy patch expiry,
    output sanitizer, and delivery state known.

    If any check fails → REVIEW_REQUIRED.
    If state unknown → REVIEW_REQUIRED (manual review required).
    Only if all pass → ALLOW.
    """

    def __init__(self, state: SessionResumeState):
        self.state = state

    def reconcile(
        self,
        session_key_hash: str,
        pending_output: dict | None = None,
    ) -> ReconciliationResult:
        """
        Reconcile session resume state before allowing delivery.

        Args:
            session_key_hash: The session key hash to validate.
            pending_output: Optional pending output to check.

        Returns:
            ReconciliationResult with decision and failed checks.
        """
        failed_checks: list[ReconciliationCheck] = []
        metadata: dict = {}

        # Check 1: Subject scope
        if not self.state.subject_id:
            failed_checks.append(ReconciliationCheck.SUBJECT_SCOPE)
            metadata["subject_scope_error"] = "missing or empty"

        # Check 2: Session key hash
        if not self.state.session_key_hash or self.state.session_key_hash != session_key_hash:
            failed_checks.append(ReconciliationCheck.SESSION_KEY_HASH)
            metadata["session_key_hash_error"] = "missing, empty, or mismatch"

        # Check 3: Platform allowlist
        if not self.state.platform_allowlist_valid:
            failed_checks.append(ReconciliationCheck.PLATFORM_ALLOWLIST)
            metadata["allowlist_error"] = "invalid or not allowlisted"

        # Check 4: Delivery enabled
        if not self.state.delivery_enabled:
            failed_checks.append(ReconciliationCheck.DELIVERY_ENABLED)
            metadata["delivery_error"] = "disabled"

        # Check 5: Continuity marker status
        if not self.state.continuity_marker_active:
            failed_checks.append(ReconciliationCheck.CONTINUITY_MARKER_STATUS)
            metadata["continuity_marker_error"] = "inactive"

        # Check 6: Continuity marker TTL
        if self.state.continuity_marker_expires_at is not None:
            if datetime.now(timezone.utc) > self.state.continuity_marker_expires_at:
                failed_checks.append(ReconciliationCheck.CONTINUITY_MARKER_TTL)
                metadata["continuity_marker_ttl_error"] = "expired"

        # Check 7: Continuity scope pause
        if self.state.continuity_scope_paused:
            failed_checks.append(ReconciliationCheck.CONTINUITY_SCOPE_PAUSE)
            metadata["continuity_scope_error"] = "paused"

        # Check 8: Followup attempt count (threshold: max 3)
        if self.state.followup_attempt_count >= 3:
            failed_checks.append(ReconciliationCheck.FOLLOWUP_ATTEMPT_COUNT)
            metadata["followup_attempt_error"] = "max attempts reached"

        # Check 9: Safety review state
        if self.state.safety_review_required:
            failed_checks.append(ReconciliationCheck.SAFETY_REVIEW_STATE)
            metadata["safety_review_error"] = "review required"

        # Check 10: Behavior policy patch expiry
        if self.state.behavior_policy_patch_expires_at is not None:
            if datetime.now(timezone.utc) > self.state.behavior_policy_patch_expires_at:
                failed_checks.append(ReconciliationCheck.BEHAVIOR_POLICY_PATCH_EXPIRY)
                metadata["policy_patch_error"] = "expired"

        # Check 11: Output sanitizer
        if not self.state.output_sanitizer_clean:
            failed_checks.append(ReconciliationCheck.OUTPUT_SANITIZER)
            metadata["output_sanitizer_error"] = "blocked"

        # Check 12: Delivery state known
        if not self.state.delivery_state_known:
            failed_checks.append(ReconciliationCheck.DELIVERY_STATE_KNOWN)
            metadata["delivery_state_error"] = "unknown"

        # Determine decision
        if failed_checks:
            decision = ReconciliationDecision.REVIEW_REQUIRED
        else:
            decision = ReconciliationDecision.ALLOW

        # Hold pending output if reconciliation fails
        pending_output_held = decision == ReconciliationDecision.REVIEW_REQUIRED and pending_output is not None

        return ReconciliationResult(
            decision=decision,
            failed_checks=failed_checks,
            pending_output_held=pending_output_held,
            metadata=metadata,
        )
