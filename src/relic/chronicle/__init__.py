"""Chronicle, unified event capture, decision tracking, and inspection for Relic.

Module: relic.chronicle
Version: chronicle/v1
Reference: docs/chronicle/agentic-development-plan.md

Public API, all other modules are internal.

Public exports:
    emit_event, emit_decision, emit_snapshot, emit_provenance_edge
    start_span
    get_trace_id, new_trace_id, set_trace_id
    register_session, register_run, get_runtime_scope_id, set_runtime_scope_id
    get_traceparent, make_traceparent
    context module (trace propagation)

Chronicle enums:
    EventCategory, RetentionPolicy, VisibilityLevel, ReasoningCapture,
    ProximityOrder, ValidationStatus, AccessKind, Severity

Models:
    Event, Decision, StateSnapshot, ProvenanceEdge, AccessLogEntry
"""
from __future__ import annotations

# Re-export enums from Relic core (source of truth: do not duplicate)
from relic.persistence import PrivacyLevel



# Re-export Chronicle-specific enums
from relic.chronicle.enums import (
    AccessKind,
    EventCategory,
    ProximityOrder,
    ReasoningCapture,
    RetentionPolicy,
    Severity,
    ValidationStatus,
    VisibilityLevel,
)

# Re-export models
from relic.chronicle.schema import (
    AccessLogEntry,
    Decision,
    Event,
    ProvenanceEdge,
    StateSnapshot,
)

# Re-export context helpers (safe: no circular dependency)
from relic.chronicle.context import (
    get_run_id,
    get_runtime_scope_id,
    get_session_id,
    get_span_id,
    get_trace_id,
    get_traceparent,
    make_traceparent,
    new_span_id,
    new_trace_id,
    register_run,
    register_session,
    set_runtime_scope_id,
    set_span_id,
    set_trace_id,
)

__all__ = [
    "PrivacyLevel",
    "EventCategory",
    "RetentionPolicy",
    "VisibilityLevel",
    "ReasoningCapture",
    "ProximityOrder",
    "ValidationStatus",
    "AccessKind",
    "Severity",
    "Event",
    "Decision",
    "StateSnapshot",
    "ProvenanceEdge",
    "AccessLogEntry",
    "get_trace_id",
    "new_trace_id",
    "set_trace_id",
    "get_traceparent",
    "make_traceparent",
    "register_session",
    "register_run",
    "get_runtime_scope_id",
    "set_runtime_scope_id",
    "get_session_id",
    "get_run_id",
    "get_span_id",
    "new_span_id",
    "set_span_id",
]

# Emitter (write-path)
from relic.chronicle.emitter import (
    emit_decision,
    emit_event,
    emit_provenance_edge,
    emit_snapshot,
    start_span,
)

__all__ += [
    "emit_event",
    "emit_decision",
    "emit_snapshot",
    "emit_provenance_edge",
    "start_span",
]

# Reader (read-path)
from relic.chronicle.reader import (
    join_trace,
    query_decisions,
    query_events,
    query_snapshots,
    stats,
)

# Provenance
from relic.chronicle.provenance import (
    add_edge,
    get_ancestors,
    get_descendants,
    verify_artifact_provenance,
)

# Retention / Reaper
from relic.chronicle.retention import (
    archive_journal,
    delete_expired,
    purge_runtime_scope_records,
    run as reaper_run,
)

# Access audit
from relic.chronicle.access_audit import log_access, log_export

# Snapshots
from relic.chronicle.snapshots import capture_snapshot

__all__ += [
    "query_events",
    "query_decisions",
    "query_snapshots",
    "join_trace",
    "stats",
    "add_edge",
    "get_ancestors",
    "get_descendants",
    "verify_artifact_provenance",
    "reaper_run",
    "archive_journal",
    "delete_expired",
    "purge_runtime_scope_records",
    "log_access",
    "log_export",
    "capture_snapshot",
]
