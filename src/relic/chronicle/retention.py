"""Chronicle retention, reaper logic for expired events/snapshots/edges.

Module: relic.chronicle.retention
Version: chronicle-retention/v1
Reference: docs/chronicle/agentic-development-plan.md §14.2, T022

Reaper policy (§9.bis):
  - EPHEMERAL: deleted after 1h
  - SHORT_30D: deleted after 30 days
  - STANDARD_365D: deleted after 365 days
  - EXTENDED_RESEARCH: never auto-delete
  - LEGAL_HOLD: never auto-delete

Reaper NEVER removes JSONL (append-only). Instead:
  1. Archival: daily JSONL → ~/.relic/user/chronicle/archive/YYYY-MM.tar.gz
  2. Deletion: only SQLite rows + orphan blobs
  3. Runtime-scope deletion: rewrites JSONL files without matching scope rows
"""
from __future__ import annotations

import logging
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time thresholds
# ---------------------------------------------------------------------------

_RETENTION_THRESHOLDS: dict[str, timedelta | None] = {
    "ephemeral": timedelta(hours=1),
    "short_30d": timedelta(days=30),
    "standard_365d": timedelta(days=365),
    "extended_research": None,  # never
    "legal_hold": None,  # never
}


def _threshold(policy: str) -> datetime | None:
    """Return cutoff datetime for a retention policy, or None if never."""
    td = _RETENTION_THRESHOLDS.get(policy)
    if td is None:
        return None
    return datetime.now(timezone.utc) - td


# ---------------------------------------------------------------------------
# Archive daily journal
# ---------------------------------------------------------------------------

def archive_journal(*, dry_run: bool = True) -> dict[str, Any]:
    """Archive today's journal JSONL to tar.gz.

    Args:
        dry_run: if True, returns counts without writing. if False, writes archive.

    Returns dict with keys: journal_size_bytes, archive_path, lines_count, archived.
    """
    from relic.chronicle.emitter import _today_journal_path, _journal_dir

    journal_path = _today_journal_path()
    results: dict[str, Any] = {
        "journal_path": str(journal_path),
        "journal_size_bytes": 0,
        "lines_count": 0,
        "archived": False,
        "archive_path": None,
    }

    if not journal_path.exists():
        return results

    size = journal_path.stat().st_size
    results["journal_size_bytes"] = size

    # Count lines
    try:
        with open(journal_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        results["lines_count"] = len(lines)
    except Exception:
        results["lines_count"] = 0

    if dry_run:
        return results

    # Create archive
    archive_dir = _journal_dir().parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m")
    archive_path = archive_dir / f"{today}.tar.gz"

    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(journal_path, arcname=journal_path.name)
        results["archived"] = True
        results["archive_path"] = str(archive_path)
        logger.info(f"[chronicle.reaper] archived {journal_path.name} → {archive_path}")
    except Exception as e:
        logger.error(f"[chronicle.reaper] archive failed: {e}")

    return results


# ---------------------------------------------------------------------------
# Delete expired records
# ---------------------------------------------------------------------------

def _get_db_connection():
    try:
        from relic.db import get_connection
        return get_connection()
    except Exception as e:
        logger.error(f"[chronicle] DB unavailable: {e}")
        raise


def delete_expired(
    *,
    dry_run: bool = True,
    policy: str | None = None,
    runtime_scope_id: str | None = None,
) -> dict[str, Any]:
    """Delete expired records, optionally restricted to one runtime scope.

    Args:
        dry_run: if True, returns counts. if False, deletes.
        policy: filter by RetentionPolicy value (default: all deletable).
        runtime_scope_id: only delete records from this canonical runtime scope.
    """
    try:
        conn = _get_db_connection()
    except Exception as e:
        return {"error": str(e)}

    results: dict[str, Any] = {
        "dry_run": dry_run,
        "policy": policy,
        "runtime_scope_id": runtime_scope_id,
        "chronicle_events_deleted": 0,
        "chronicle_decisions_deleted": 0,
        "chronicle_state_snapshots_deleted": 0,
        "chronicle_provenance_edges_deleted": 0,
        "orphan_blobs_deleted": 0,
    }

    # Build WHERE clause for retention policy
    deletable_policies = ["ephemeral", "short_30d", "standard_365d"]
    if policy:
        if policy not in deletable_policies:
            return results  # nothing to delete for non-deletable policy
        deletable_policies = [policy]

    # Compute cutoff for each policy
    cutoffs = {}
    for pol in deletable_policies:
        cutoff = _threshold(pol)
        if cutoff:
            cutoffs[pol] = cutoff.isoformat()

    if not cutoffs:
        return results  # all policies are non-deletable

    # Build condition. Param order must match the WHERE placeholders.
    runtime_scope_clause = (
        " AND runtime_scope_id = ?" if runtime_scope_id else ""
    )

    def _delete_table(table: str, where_clause: str, params_list: list) -> int:
        """Delete from table and return count."""
        if dry_run:
            try:
                c = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_clause}", params_list)
                return c.fetchone()[0]
            except Exception:
                return 0
        else:
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE {where_clause}", params_list)
                conn.commit()
                return cur.rowcount
            except Exception as e:
                logger.error(f"[chronicle.reaper] delete from {table}: {e}")
                return 0

    # Events: placeholder order: retention_policy, timestamp, runtime_scope_id.
    for pol, cutoff in cutoffs.items():
        p = [pol, cutoff] + ([runtime_scope_id] if runtime_scope_id else [])
        count = _delete_table(
            "chronicle_events",
            "retention_policy = ? AND timestamp < ?" + runtime_scope_clause,
            p,
        )
        results["chronicle_events_deleted"] += count

    # Decisions
    for pol, cutoff in cutoffs.items():
        p = [pol, cutoff] + ([runtime_scope_id] if runtime_scope_id else [])
        count = _delete_table(
            "chronicle_decisions",
            "retention_policy = ? AND timestamp < ?" + runtime_scope_clause,
            p,
        )
        results["chronicle_decisions_deleted"] += count

    # Snapshots use captured_at rather than timestamp.
    for pol, cutoff in cutoffs.items():
        p = [pol, cutoff] + ([runtime_scope_id] if runtime_scope_id else [])
        count = _delete_table(
            "chronicle_state_snapshots",
            "retention_policy = ? AND captured_at < ?" + runtime_scope_clause,
            p,
        )
        results["chronicle_state_snapshots_deleted"] += count

    # Provenance edges are only reaped when orphaned.
    if runtime_scope_id and not dry_run:
        try:
            cur = conn.execute(
                """
                DELETE FROM chronicle_provenance_edges
                WHERE runtime_scope_id = ?
                AND artifact_id NOT IN (SELECT id FROM artifact_records)
                AND from_node_id NOT IN (SELECT event_id FROM chronicle_events)
                """,
                (runtime_scope_id,),
            )
            conn.commit()
            results["chronicle_provenance_edges_deleted"] = cur.rowcount
        except Exception as e:
            logger.error(f"[chronicle.reaper] orphan edge delete: {e}")

    return results


# ---------------------------------------------------------------------------
# Reaper run (main entry point)
# ---------------------------------------------------------------------------

def run(
    *,
    dry_run: bool = True,
    policy: str | None = None,
    runtime_scope_id: str | None = None,
    archive_journals: bool = True,
) -> dict[str, Any]:
    """Run reaper: archive journals + delete expired records."""
    results: dict[str, Any] = {
        "dry_run": dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if archive_journals:
        results["archive"] = archive_journal(dry_run=dry_run)

    results["deletion"] = delete_expired(
        dry_run=dry_run,
        policy=policy,
        runtime_scope_id=runtime_scope_id,
    )

    total = (
        results["deletion"].get("chronicle_events_deleted", 0)
        + results["deletion"].get("chronicle_decisions_deleted", 0)
        + results["deletion"].get("chronicle_state_snapshots_deleted", 0)
        + results["deletion"].get("chronicle_provenance_edges_deleted", 0)
    )
    results["total_deleted"] = total
    results["summary"] = f"dry_run={dry_run}: {total} records"

    logger.info(f"[chronicle.reaper] {'DRY-RUN ' if dry_run else ''}{results['summary']}")

    return results


# ---------------------------------------------------------------------------
# Runtime-scope hard delete
# ---------------------------------------------------------------------------

def _rewrite_jsonl_without_runtime_scope(
    path: Path, runtime_scope_id: str
) -> int:
    """Atomically remove JSONL rows belonging to one runtime scope."""
    import json
    import tempfile

    removed = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()

        kept: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            try:
                if json.loads(stripped).get("runtime_scope_id") == runtime_scope_id:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
            kept.append(line)

        if removed:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=".runtime_scope_purge_"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
                os.replace(tmp_name, str(path))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
    except Exception as exc:
        raise RuntimeError(f"JSONL rewrite failed for {path}") from exc

    return removed


def _rewrite_jsonl_without_subject(path: Path, subject_id: str) -> int:
    """Atomically remove JSONL rows belonging to one legacy subject."""
    import json
    import tempfile

    removed = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()

        kept: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            try:
                if json.loads(stripped).get("subject_id") == subject_id:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
            kept.append(line)

        if removed:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=".subject_purge_"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
                os.replace(tmp_name, str(path))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
    except Exception as exc:
        raise RuntimeError(f"JSONL rewrite failed for {path}") from exc

    return removed


def purge_legacy_subject_records(
    subject_id: str,
    *,
    relic_home: Path | None = None,
    cascade: bool = False,
) -> dict[str, Any]:
    """Irreversibly remove records from pre-runtime-scope Chronicle schemas."""
    from relic.paths import get_relic_home as _get_relic_home

    home = relic_home or _get_relic_home()
    results: dict[str, Any] = {
        "subject_id": subject_id,
        "chronicle_events_deleted": 0,
        "chronicle_decisions_deleted": 0,
        "chronicle_state_snapshots_deleted": 0,
        "chronicle_access_log_deleted": 0,
        "chronicle_provenance_edges_deleted": 0,
        "journal_files_rewritten": 0,
        "journal_lines_removed": 0,
        "legacy_jsonl_lines_removed": 0,
    }

    try:
        conn = _get_db_connection()
        if cascade:
            cur = conn.execute(
                """
                DELETE FROM chronicle_provenance_edges
                WHERE from_node_id IN (
                    SELECT event_id FROM chronicle_events WHERE subject_id = ?
                )
                OR trace_id IN (
                    SELECT trace_id FROM chronicle_events WHERE subject_id = ?
                    UNION
                    SELECT trace_id FROM chronicle_decisions WHERE subject_id = ?
                    UNION
                    SELECT trace_id FROM chronicle_state_snapshots WHERE subject_id = ?
                )
                """,
                (subject_id, subject_id, subject_id, subject_id),
            )
            results["chronicle_provenance_edges_deleted"] = cur.rowcount
        for table, key in [
            ("chronicle_events", "chronicle_events_deleted"),
            ("chronicle_decisions", "chronicle_decisions_deleted"),
            ("chronicle_state_snapshots", "chronicle_state_snapshots_deleted"),
        ]:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE subject_id = ?",
                (subject_id,),
            )
            results[key] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM chronicle_access_log WHERE target_filter LIKE ?",
            (f'%"{subject_id}"%',),
        )
        results["chronicle_access_log_deleted"] = cur.rowcount
        conn.commit()
    except Exception as exc:
        raise RuntimeError("chronicle database purge failed") from exc

    journal_dir = home / "chronicle" / "journal"
    if journal_dir.exists():
        for journal_path in sorted(journal_dir.glob("*.jsonl")):
            removed = _rewrite_jsonl_without_subject(journal_path, subject_id)
            if removed:
                results["journal_files_rewritten"] += 1
                results["journal_lines_removed"] += removed

    legacy_total = 0
    for fname in (
        "decision_events.jsonl",
        "cac_trace.jsonl",
        "privacy_trace.jsonl",
        "escalation_log.jsonl",
    ):
        path = home / fname
        if path.exists():
            legacy_total += _rewrite_jsonl_without_subject(path, subject_id)
    results["legacy_jsonl_lines_removed"] = legacy_total

    logger.info(
        "[chronicle.purge] legacy subject rows deleted events=%d decisions=%d "
        "snapshots=%d access=%d journal_lines=%d legacy_lines=%d",
        results["chronicle_events_deleted"],
        results["chronicle_decisions_deleted"],
        results["chronicle_state_snapshots_deleted"],
        results["chronicle_access_log_deleted"],
        results["journal_lines_removed"],
        results["legacy_jsonl_lines_removed"],
    )
    return results


def purge_runtime_scope_records(
    runtime_scope_id: str,
    *,
    relic_home: Path | None = None,
    cascade: bool = False,
) -> dict[str, Any]:
    """Irreversibly remove all Chronicle records for one runtime scope."""
    from relic.chronicle.emitter import (
        LegacyChronicleMigrationRequired,
        _require_runtime_scope_schema,
    )
    from relic.paths import get_relic_home as _get_relic_home

    for table in (
        "chronicle_events",
        "chronicle_decisions",
        "chronicle_state_snapshots",
        "chronicle_provenance_edges",
    ):
        _require_runtime_scope_schema(table)

    home = relic_home or _get_relic_home()
    results: dict[str, Any] = {
        "runtime_scope_id": runtime_scope_id,
        "chronicle_events_deleted": 0,
        "chronicle_decisions_deleted": 0,
        "chronicle_state_snapshots_deleted": 0,
        "chronicle_access_log_deleted": 0,
        "chronicle_provenance_edges_deleted": 0,
        "journal_files_rewritten": 0,
        "journal_lines_removed": 0,
        "legacy_jsonl_lines_removed": 0,
    }

    try:
        conn = _get_db_connection()
        if cascade:
            cur = conn.execute(
                "DELETE FROM chronicle_provenance_edges WHERE runtime_scope_id = ?",
                (runtime_scope_id,),
            )
            results["chronicle_provenance_edges_deleted"] = cur.rowcount
        for table, key in [
            ("chronicle_events", "chronicle_events_deleted"),
            ("chronicle_decisions", "chronicle_decisions_deleted"),
            ("chronicle_state_snapshots", "chronicle_state_snapshots_deleted"),
        ]:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE runtime_scope_id = ?",
                (runtime_scope_id,),
            )
            results[key] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM chronicle_access_log WHERE target_filter LIKE ?",
            (f'%"{runtime_scope_id}"%',),
        )
        results["chronicle_access_log_deleted"] = cur.rowcount
        conn.commit()
    except LegacyChronicleMigrationRequired:
        raise
    except Exception as exc:
        raise RuntimeError("chronicle database purge failed") from exc

    journal_dir = home / "user" / "chronicle" / "journal"
    if journal_dir.exists():
        for journal_path in sorted(journal_dir.glob("*.jsonl")):
            removed = _rewrite_jsonl_without_runtime_scope(
                journal_path, runtime_scope_id
            )
            if removed:
                results["journal_files_rewritten"] += 1
                results["journal_lines_removed"] += removed

    legacy_total = 0
    for fname in (
        "decision_events.jsonl",
        "cac_trace.jsonl",
        "privacy_trace.jsonl",
        "escalation_log.jsonl",
    ):
        path = home / fname
        if path.exists():
            legacy_total += _rewrite_jsonl_without_runtime_scope(path, runtime_scope_id)
    results["legacy_jsonl_lines_removed"] = legacy_total

    logger.info(
        "[chronicle.purge] runtime_scope=%s deleted events=%d decisions=%d "
        "snapshots=%d access=%d journal_lines=%d legacy_lines=%d",
        runtime_scope_id,
        results["chronicle_events_deleted"],
        results["chronicle_decisions_deleted"],
        results["chronicle_state_snapshots_deleted"],
        results["chronicle_access_log_deleted"],
        results["journal_lines_removed"],
        results["legacy_jsonl_lines_removed"],
    )
    return results
