"""Chronicle reader, read-path for events, decisions, snapshots, provenance edges.

Module: relic.chronicle.reader
Version: chronicle-reader/v1
Reference: docs/chronicle/agentic-development-plan.md §8.1, T070

All queries read from SQLite source-of-truth. JSONL is never read by the reader.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _get_db_connection():
    try:
        from relic.db import get_connection
        return get_connection()
    except Exception as e:
        logger.error(f"[chronicle.reader] DB unavailable: {e}")
        raise


# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------

def query_events(
    *,
    trace_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    run_id: uuid.UUID | str | None = None,
    runtime_scope_id: str | None = None,
    event_type: str | None = None,
    event_category: str | None = None,
    sensitivity: str | None = None,
    severity: str | None = None,
    source_module: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query chronicle_events with filters. Returns list of row dicts."""
    try:
        conn = _get_db_connection()
    except Exception:
        return []

    conditions = ["1=1"]
    params: list[Any] = []
    if trace_id:
        conditions.append("trace_id = ?")
        params.append(str(trace_id))
    if session_id:
        conditions.append("session_id = ?")
        params.append(str(session_id))
    if run_id:
        conditions.append("run_id = ?")
        params.append(str(run_id))
    if runtime_scope_id:
        conditions.append("runtime_scope_id = ?")
        params.append(runtime_scope_id)
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if event_category:
        conditions.append("event_category = ?")
        params.append(event_category)
    if sensitivity:
        conditions.append("sensitivity = ?")
        params.append(sensitivity)
    if severity:
        conditions.append("severity = ?")
        params.append(severity)
    if source_module:
        conditions.append("source_module LIKE ?")
        params.append(f"{source_module}%")
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)
    if until:
        conditions.append("timestamp <= ?")
        params.append(until)

    sql = f"""
        SELECT event_id, runtime_scope_id, event_type, event_category, trace_id,
               run_id, session_id, parent_event_id, agent_id, hermes_profile_id,
               actor_type, actor_id, source_module, target_module, timestamp,
               duration_ms, input_refs, output_refs, payload_redacted, payload_hash,
               payload, sensitivity, retention_policy, tags, severity,
               validation_status, error_code, retry_count, schema_version, created_at
        FROM chronicle_events
        WHERE {" AND ".join(conditions)}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    columns = [
        "event_id",
        "runtime_scope_id",
        "event_type",
        "event_category",
        "trace_id",
        "run_id",
        "session_id",
        "parent_event_id",
        "agent_id",
        "hermes_profile_id",
        "actor_type",
        "actor_id",
        "source_module",
        "target_module",
        "timestamp",
        "duration_ms",
        "input_refs",
        "output_refs",
        "payload_redacted",
        "payload_hash",
        "payload",
        "sensitivity",
        "retention_policy",
        "tags",
        "severity",
        "validation_status",
        "error_code",
        "retry_count",
        "schema_version",
        "created_at",
    ]
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        logger.error(f"[chronicle.reader] query_events failed: {exc}")
        return []


def query_decisions(
    *,
    trace_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    runtime_scope_id: str | None = None,
    decision_kind: str | None = None,
    actor_id: str | None = None,
    validation_status: str | None = None,
    since: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query chronicle_decisions with filters."""
    try:
        conn = _get_db_connection()
    except Exception:
        return []

    conditions = ["1=1"]
    params: list[Any] = []
    if trace_id:
        conditions.append("trace_id = ?")
        params.append(str(trace_id))
    if session_id:
        conditions.append("session_id = ?")
        params.append(str(session_id))
    if runtime_scope_id:
        conditions.append("runtime_scope_id = ?")
        params.append(runtime_scope_id)
    if decision_kind:
        conditions.append("decision_kind = ?")
        params.append(decision_kind)
    if actor_id:
        conditions.append("actor_id = ?")
        params.append(actor_id)
    if validation_status:
        conditions.append("validation_status = ?")
        params.append(validation_status)
    if since:
        conditions.append("timestamp >= ?")
        params.append(since)

    sql = f"""
        SELECT decision_id, runtime_scope_id, trace_id, run_id, session_id,
               actor_type, actor_id, decision_kind, selected_action,
               rejected_alternatives, observable_inputs, observable_outputs,
               confidence, uncertainty_notes, evidence_refs, rationale_summary,
               sensitivity, retention_policy, validation_status, timestamp,
               schema_version, created_at
        FROM chronicle_decisions
        WHERE {" AND ".join(conditions)}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    columns = [
        "decision_id",
        "runtime_scope_id",
        "trace_id",
        "run_id",
        "session_id",
        "actor_type",
        "actor_id",
        "decision_kind",
        "selected_action",
        "rejected_alternatives",
        "observable_inputs",
        "observable_outputs",
        "confidence",
        "uncertainty_notes",
        "evidence_refs",
        "rationale_summary",
        "sensitivity",
        "retention_policy",
        "validation_status",
        "timestamp",
        "schema_version",
        "created_at",
    ]
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        logger.error(f"[chronicle.reader] query_decisions failed: {exc}")
        return []


def query_snapshots(
    *,
    runtime_scope_id: str | None = None,
    snapshot_type: str | None = None,
    scope_ref: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Query chronicle_state_snapshots."""
    try:
        conn = _get_db_connection()
    except Exception:
        return []

    conditions = ["1=1"]
    params: list[Any] = []
    if runtime_scope_id:
        conditions.append("runtime_scope_id = ?")
        params.append(runtime_scope_id)
    if snapshot_type:
        conditions.append("snapshot_type = ?")
        params.append(snapshot_type)
    if scope_ref:
        conditions.append("scope_ref = ?")
        params.append(scope_ref)
    if since:
        conditions.append("captured_at >= ?")
        params.append(since)

    sql = f"""
        SELECT snapshot_id, runtime_scope_id, snapshot_type, scope_ref, trace_id,
               captured_at, trigger_event_id, previous_snapshot_id, content_hash,
               content_ref, content_size_bytes, diff_from_previous, sensitivity,
               retention_policy, schema_version, created_at
        FROM chronicle_state_snapshots
        WHERE {" AND ".join(conditions)}
        ORDER BY captured_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    columns = [
        "snapshot_id",
        "runtime_scope_id",
        "snapshot_type",
        "scope_ref",
        "trace_id",
        "captured_at",
        "trigger_event_id",
        "previous_snapshot_id",
        "content_hash",
        "content_ref",
        "content_size_bytes",
        "diff_from_previous",
        "sensitivity",
        "retention_policy",
        "schema_version",
        "created_at",
    ]
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        logger.error(f"[chronicle.reader] query_snapshots failed: {exc}")
        return []


# ---------------------------------------------------------------------------
# Join queries
# ---------------------------------------------------------------------------

def join_trace(
    trace_id: uuid.UUID | str,
    *,
    include_events: bool = True,
    include_decisions: bool = True,
    include_snapshots: bool = False,
) -> dict[str, Any]:
    """Retrieve all records for a given trace_id, grouped by type."""
    trace_id_str = str(trace_id)
    result: dict[str, Any] = {
        "trace_id": trace_id_str,
        "events": [],
        "decisions": [],
        "snapshots": [],
        "stats": {},
    }

    if include_events:
        result["events"] = query_events(trace_id=trace_id_str, limit=1000)

    if include_decisions:
        result["decisions"] = query_decisions(trace_id=trace_id_str, limit=1000)

    if include_snapshots:
        # Find session_id from events to filter snapshots
        session_ids = list(set(e.get("session_id") for e in result["events"] if e.get("session_id")))
        for sid in session_ids:
            snaps = query_snapshots(scope_ref=f"session:{sid}", limit=100)
            result["snapshots"].extend(snaps)

    # Compute stats
    result["stats"] = {
        "events_count": len(result["events"]),
        "decisions_count": len(result["decisions"]),
        "snapshots_count": len(result["snapshots"]),
        "event_types": list(set(e.get("event_type", "") for e in result["events"])),
        "decision_kinds": list(set(d.get("decision_kind", "") for d in result["decisions"])),
        "time_span": {
            "first": (result["events"][-1]["timestamp"] if result["events"] else None),
            "last": (result["events"][0]["timestamp"] if result["events"] else None),
        },
    }

    return result


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats(
    *,
    runtime_scope_id: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Compute aggregate statistics for events/decisions."""
    try:
        conn = _get_db_connection()
    except Exception:
        return {}

    base_where = "1=1"
    params: list[Any] = []
    if runtime_scope_id:
        base_where += " AND runtime_scope_id = ?"
        params.append(runtime_scope_id)
    if since:
        base_where += " AND timestamp >= ?"
        params.append(since)

    result: dict[str, Any] = {}
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM chronicle_events WHERE {base_where}", params
        ).fetchone()
        result["total_events"] = row[0] if row else 0

        rows = conn.execute(
            f"SELECT event_type, COUNT(*) AS cnt FROM chronicle_events "
            f"WHERE {base_where} GROUP BY event_type ORDER BY cnt DESC LIMIT 20",
            params,
        ).fetchall()
        result["by_event_type"] = [{"type": row[0], "count": row[1]} for row in rows]

        rows = conn.execute(
            f"SELECT severity, COUNT(*) AS cnt FROM chronicle_events "
            f"WHERE {base_where} GROUP BY severity ORDER BY cnt DESC",
            params,
        ).fetchall()
        result["by_severity"] = [
            {"severity": row[0], "count": row[1]} for row in rows
        ]

        rows = conn.execute(
            f"SELECT sensitivity, COUNT(*) AS cnt FROM chronicle_events "
            f"WHERE {base_where} GROUP BY sensitivity ORDER BY cnt DESC",
            params,
        ).fetchall()
        result["by_sensitivity"] = [
            {"sensitivity": row[0], "count": row[1]} for row in rows
        ]

        row = conn.execute(
            f"SELECT COUNT(*) FROM chronicle_decisions WHERE {base_where}", params
        ).fetchone()
        result["total_decisions"] = row[0] if row else 0

        snapshot_where = base_where.replace("timestamp", "captured_at")
        row = conn.execute(
            f"SELECT COUNT(*) FROM chronicle_state_snapshots WHERE {snapshot_where}",
            params,
        ).fetchone()
        result["total_snapshots"] = row[0] if row else 0
    except Exception as exc:
        logger.error(f"[chronicle.reader] stats failed: {exc}")

    return result
