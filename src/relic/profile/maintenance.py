"""Deterministic snapshots and report finalization for Gumi maintenance cron."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Hermes Gumi runs on POSIX
    fcntl = None  # type: ignore[assignment]

CANON_FILES = (
    "world.md",
    "voice_canon.json",
    "visual_canon.json",
    "lyria_canon.json",
    "media_policy.json",
    "relationship_policy.md",
)
MAX_CONTEXT_TOKENS = 8_000

_KINDS = {
    "state_compaction": (
        ".state_compaction_baseline.json",
        ".state_compaction_pending.json",
        "world_state_compaction_report.json",
    ),
    "candidate_review": (
        ".candidate_review_baseline.json",
        ".candidate_review_pending.json",
        "continuity_candidate_review_report.json",
    ),
}
_MAX_FILE_BYTES = 512_000
_MAX_NOTE_CHARS = 2_000
_PENDING_TTL = timedelta(hours=6)


class MaintenanceError(RuntimeError):
    """A maintenance snapshot or report cannot be accepted safely."""


def _config(kind: str) -> tuple[str, str, str]:
    try:
        return _KINDS[kind]
    except KeyError as exc:
        raise MaintenanceError("unsupported maintenance kind") from exc


def _workspace(workspace: Path) -> tuple[Path, Path]:
    workspace = Path(workspace).expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise MaintenanceError(f"maintenance workspace is unavailable: {workspace}")
    cron = workspace / "cron"
    if cron.is_symlink():
        raise MaintenanceError(f"maintenance cron directory is unsafe: {cron}")
    cron.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cron, 0o700)
    return workspace, cron


@contextmanager
def _job_lock(cron: Path, kind: str):
    lock_path = cron / f".{kind}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise MaintenanceError(f"unsafe maintenance path: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError(f"unsafe maintenance state file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"invalid maintenance state file: {path}") from exc
    if not isinstance(value, dict):
        raise MaintenanceError(f"invalid maintenance state file: {path}")
    return value


def _snapshot(workspace: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    hashes: dict[str, str | None] = {}
    missing: list[str] = []
    for filename in CANON_FILES:
        path = workspace / filename
        if path.is_symlink():
            raise MaintenanceError(f"canonical file is unsafe: {filename}")
        if not path.exists():
            hashes[filename] = None
            missing.append(filename)
            continue
        if path.is_symlink() or not path.is_file():
            raise MaintenanceError(f"canonical file is unsafe: {filename}")
        try:
            raw = path.read_bytes()
            if len(raw) > _MAX_FILE_BYTES:
                raise MaintenanceError(f"canonical file is too large: {filename}")
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise MaintenanceError(f"canonical file is unreadable: {filename}") from exc
        files[filename] = content
        hashes[filename] = hashlib.sha256(raw).hexdigest()
    return {"files": files, "hashes": hashes, "missing_files": missing}


def _snapshot_id(hashes: dict[str, str | None]) -> str:
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _estimate_context_tokens(snapshot: dict[str, Any], kind: str, changed_files: list[str]) -> int:
    payload = {
        "kind": kind,
        "snapshot_id": _snapshot_id(snapshot["hashes"]),
        "hashes": snapshot["hashes"],
        "changed_files": changed_files,
        "missing_files": snapshot["missing_files"],
        "files": snapshot["files"],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (len(serialized) + 3) // 4


def _valid_snapshot_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MaintenanceError("maintenance snapshot id is invalid")
    return value


def _validate_hashes(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != set(CANON_FILES):
        raise MaintenanceError("maintenance snapshot hashes are invalid")
    for filename, digest in value.items():
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise MaintenanceError(f"maintenance hash is invalid: {filename}")
    return value


def prepare_run(workspace: Path, kind: str) -> dict[str, Any]:
    baseline_name, pending_name, report_name = _config(kind)
    workspace, cron = _workspace(workspace)
    with _job_lock(cron, kind):
        snapshot = _snapshot(workspace)
        baseline = _read_json(cron / baseline_name)
        baseline_hashes = _validate_hashes(baseline["hashes"]) if baseline else None
        changed_files = (
            list(CANON_FILES)
            if baseline_hashes is None
            else [
                filename
                for filename in CANON_FILES
                if baseline_hashes.get(filename) != snapshot["hashes"].get(filename)
            ]
        )
        report_path = cron / report_name
        report_exists = report_path.is_file() and not report_path.is_symlink()
        if not changed_files and report_exists:
            return {
                "wakeAgent": False,
                "kind": kind,
                "status": "no-change",
                "snapshot_id": _snapshot_id(snapshot["hashes"]),
                "changed_files": [],
            }

        existing_pending = _read_json(cron / pending_name)
        if existing_pending is not None:
            prepared_at = existing_pending.get("prepared_at")
            try:
                prepared = datetime.fromisoformat(str(prepared_at))
                if prepared.tzinfo is None:
                    prepared = prepared.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError) as exc:
                raise MaintenanceError("maintenance pending snapshot is invalid") from exc
            if datetime.now(timezone.utc) - prepared < _PENDING_TTL:
                snapshot_id = _valid_snapshot_id(existing_pending.get("snapshot_id"))
                _validate_hashes(existing_pending.get("hashes"))
                return {
                    "wakeAgent": False,
                    "kind": kind,
                    "status": "pending",
                    "snapshot_id": snapshot_id,
                    "changed_files": existing_pending.get("changed_files", []),
                }
            (cron / pending_name).unlink()

        estimated_context_tokens = _estimate_context_tokens(
            snapshot, kind, changed_files
        )
        if estimated_context_tokens > MAX_CONTEXT_TOKENS:
            raise MaintenanceError(
                "maintenance snapshot exceeds 8,000-token budget"
            )

        pending = {
            "kind": kind,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": _snapshot_id(snapshot["hashes"]),
            "hashes": snapshot["hashes"],
            "changed_files": changed_files,
            "estimated_context_tokens": estimated_context_tokens,
        }
        _atomic_json(cron / pending_name, pending)
        return {
            "wakeAgent": True,
            "kind": kind,
            "status": "changed",
            "snapshot_id": pending["snapshot_id"],
            "hashes": snapshot["hashes"],
            "changed_files": changed_files,
            "missing_files": snapshot["missing_files"],
            "files": snapshot["files"],
            "estimated_context_tokens": estimated_context_tokens,
        }


def _parse_response(response: str) -> dict[str, Any]:
    text = str(response or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
        if text.lower().startswith("json\n"):
            text = text[5:].lstrip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MaintenanceError("maintenance model output is not JSON") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("maintenance model output must be an object")
    return value


def _validate_report(
    kind: str,
    value: dict[str, Any],
    *,
    expected_snapshot_id: str,
    missing_files: list[str],
) -> dict[str, Any]:
    snapshot_id = _valid_snapshot_id(value.get("snapshot_id"))
    if snapshot_id != expected_snapshot_id:
        raise MaintenanceError("maintenance response belongs to another snapshot")
    if kind == "state_compaction":
        if value.get("status") not in {"ok", "candidates_found"}:
            raise MaintenanceError("invalid state compaction status")
        candidates = value.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > len(CANON_FILES):
            raise MaintenanceError("invalid state compaction candidates")
        for item in candidates:
            if not isinstance(item, dict) or item.get("file") not in CANON_FILES:
                raise MaintenanceError("invalid state compaction candidate")
            if not isinstance(item.get("note"), str) or not item["note"].strip():
                raise MaintenanceError("invalid state compaction note")
            if len(item["note"]) > _MAX_NOTE_CHARS:
                raise MaintenanceError("state compaction note is too long")
        if (value["status"] == "ok") != (not candidates):
            raise MaintenanceError("state compaction status does not match candidates")
        candidate_files = {item["file"] for item in candidates}
        if missing_files and (
            value["status"] != "candidates_found"
            or not set(missing_files).issubset(candidate_files)
        ):
            raise MaintenanceError("missing canonical files must be reported as candidates")
        return {
            "snapshot_id": snapshot_id,
            "status": value["status"],
            "candidates": candidates,
        }

    if value.get("status") not in {"ok", "drift"}:
        raise MaintenanceError("invalid candidate review status")
    findings = value.get("findings")
    if not isinstance(findings, list) or len(findings) > 100:
        raise MaintenanceError("invalid candidate review findings")
    if not all(
        isinstance(item, str) and item.strip() and len(item) <= _MAX_NOTE_CHARS
        for item in findings
    ):
        raise MaintenanceError("invalid candidate review finding")
    if (value["status"] == "ok") != (not findings):
        raise MaintenanceError("candidate review status does not match findings")
    if missing_files and (
        value["status"] != "drift"
        or not all(
            any(filename in finding for finding in findings) for filename in missing_files
        )
    ):
        raise MaintenanceError("missing canonical files must be reported as drift")
    return {"snapshot_id": snapshot_id, "status": value["status"], "findings": findings}


def finalize_run(workspace: Path, kind: str, response: str) -> dict[str, Any]:
    baseline_name, pending_name, report_name = _config(kind)
    workspace, cron = _workspace(workspace)
    with _job_lock(cron, kind):
        pending = _read_json(cron / pending_name)
        if pending is None:
            raise MaintenanceError("maintenance run has no pending snapshot")
        pending_hashes = _validate_hashes(pending.get("hashes"))
        pending_snapshot_id = _valid_snapshot_id(pending.get("snapshot_id"))
        current = _snapshot(workspace)
        normalized = _validate_report(
            kind,
            _parse_response(response),
            expected_snapshot_id=pending_snapshot_id,
            missing_files=current["missing_files"],
        )
        stale = current["hashes"] != pending_hashes
        timestamp = datetime.now(timezone.utc).isoformat()
        report = {
            "ts": timestamp,
            **normalized,
            "snapshot_id": pending_snapshot_id,
            "snapshot_hashes": pending_hashes,
            "changed_files": pending.get("changed_files", []),
            "missing_files": current["missing_files"],
            "stale_snapshot": stale,
        }
        _atomic_json(cron / report_name, report)
        if not stale:
            _atomic_json(
                cron / baseline_name,
                {"updated_at": timestamp, "hashes": current["hashes"]},
            )
        (cron / pending_name).unlink(missing_ok=True)
        return report
