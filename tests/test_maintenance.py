import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from relic.profile.maintenance import (
    CANON_FILES,
    MAX_CONTEXT_TOKENS,
    MaintenanceError,
    finalize_run,
    prepare_run,
)
from relic.profile.user_runtime import _continuity_context


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace" / "gumi"
    workspace.mkdir(parents=True)
    for filename in CANON_FILES:
        (workspace / filename).write_text(f"{filename}\n", encoding="utf-8")
    return workspace


def test_first_run_passes_full_snapshot_and_does_not_advance_baseline(tmp_path):
    workspace = _workspace(tmp_path)

    run = prepare_run(workspace, "state_compaction")

    assert run["wakeAgent"] is True
    assert set(run["files"]) == set(CANON_FILES)
    assert set(run["changed_files"]) == set(CANON_FILES)
    assert not (workspace / "cron" / ".state_compaction_baseline.json").exists()


def test_snapshot_has_a_deterministic_eight_k_token_budget(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "world.md").write_text("x" * (MAX_CONTEXT_TOKENS * 5), encoding="utf-8")

    with pytest.raises(MaintenanceError, match="8,000-token budget"):
        prepare_run(workspace, "state_compaction")


def test_valid_report_advances_only_its_job_baseline_and_next_run_is_skipped(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "state_compaction")

    report = finalize_run(
        workspace,
        "state_compaction",
        json.dumps(
            {"snapshot_id": run["snapshot_id"], "status": "ok", "candidates": []}
        ),
    )

    assert report["status"] == "ok"
    assert report["stale_snapshot"] is False
    assert prepare_run(workspace, "state_compaction")["wakeAgent"] is False
    assert prepare_run(workspace, "candidate_review")["wakeAgent"] is True


def test_state_compaction_accepts_json_code_fence_from_model(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "state_compaction")

    report = finalize_run(
        workspace,
        "state_compaction",
        "```json\n"
        + json.dumps({"snapshot_id": run["snapshot_id"], "status": "ok", "candidates": []})
        + "\n```",
    )

    assert report["status"] == "ok"
    assert report["stale_snapshot"] is False
    assert (workspace / "cron" / ".state_compaction_baseline.json").is_file()


def test_one_changed_file_passes_all_canon_files_again(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "state_compaction")
    finalize_run(
        workspace,
        "state_compaction",
        json.dumps({"snapshot_id": run["snapshot_id"], "status": "ok", "candidates": []}),
    )

    (workspace / "world.md").write_text("changed\n", encoding="utf-8")
    run = prepare_run(workspace, "state_compaction")

    assert run["wakeAgent"] is True
    assert run["changed_files"] == ["world.md"]
    assert set(run["files"]) == set(CANON_FILES)


def test_missing_canon_is_reported_in_snapshot(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "lyria_canon.json").unlink()

    run = prepare_run(workspace, "candidate_review")

    assert run["wakeAgent"] is True
    assert run["missing_files"] == ["lyria_canon.json"]
    assert "lyria_canon.json" not in run["files"]


def test_pending_snapshot_is_not_overwritten_by_a_second_prepare(tmp_path):
    workspace = _workspace(tmp_path)
    first = prepare_run(workspace, "state_compaction")
    second = prepare_run(workspace, "state_compaction")

    assert second["wakeAgent"] is False
    assert second["status"] == "pending"
    assert second["snapshot_id"] == first["snapshot_id"]


def test_snapshot_id_mismatch_does_not_advance_baseline(tmp_path):
    workspace = _workspace(tmp_path)
    prepare_run(workspace, "state_compaction")

    with pytest.raises(MaintenanceError, match="another snapshot"):
        finalize_run(
            workspace,
            "state_compaction",
            json.dumps(
                {
                    "snapshot_id": "0" * 64,
                    "status": "ok",
                    "candidates": [],
                }
            ),
        )

    assert not (workspace / "cron" / ".state_compaction_baseline.json").exists()


def test_symlinked_canon_is_rejected(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "world.md").unlink()
    (workspace / "world.md").symlink_to(workspace / "voice_canon.json")

    with pytest.raises(MaintenanceError, match="unsafe"):
        prepare_run(workspace, "state_compaction")


def test_missing_canon_cannot_advance_a_clean_baseline(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "lyria_canon.json").unlink()
    run = prepare_run(workspace, "state_compaction")

    with pytest.raises(MaintenanceError, match="missing canonical files"):
        finalize_run(
            workspace,
            "state_compaction",
            json.dumps(
                {
                    "snapshot_id": run["snapshot_id"],
                    "status": "ok",
                    "candidates": [],
                }
            ),
        )

    assert not (workspace / "cron" / ".state_compaction_baseline.json").exists()


def test_invalid_report_does_not_advance_baseline_or_write_report(tmp_path):
    workspace = _workspace(tmp_path)
    prepare_run(workspace, "candidate_review")

    with pytest.raises(MaintenanceError):
        finalize_run(workspace, "candidate_review", '{"status":"drift"}')

    cron = workspace / "cron"
    assert not (cron / ".candidate_review_baseline.json").exists()
    assert not (cron / "continuity_candidate_review_report.json").exists()


def test_candidate_review_ok_requires_empty_findings(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "candidate_review")

    with pytest.raises(MaintenanceError, match="status does not match findings"):
        finalize_run(
            workspace,
            "candidate_review",
            json.dumps(
                {
                    "snapshot_id": run["snapshot_id"],
                    "status": "ok",
                    "findings": ["Non-actionable consistency observation"],
                }
            ),
        )


def test_stale_snapshot_writes_marked_report_without_advancing_baseline(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "candidate_review")
    (workspace / "world.md").write_text("changed while model ran\n", encoding="utf-8")

    report = finalize_run(
        workspace,
        "candidate_review",
        json.dumps(
            {"snapshot_id": run["snapshot_id"], "status": "ok", "findings": []}
        ),
    )

    assert report["stale_snapshot"] is True
    assert not (workspace / "cron" / ".candidate_review_baseline.json").exists()
    assert prepare_run(workspace, "candidate_review")["wakeAgent"] is True


def test_report_and_baseline_are_private_files(tmp_path):
    workspace = _workspace(tmp_path)
    run = prepare_run(workspace, "state_compaction")
    finalize_run(
        workspace,
        "state_compaction",
        json.dumps({"snapshot_id": run["snapshot_id"], "status": "ok", "candidates": []}),
    )

    for filename in (
        ".state_compaction_baseline.json",
        "world_state_compaction_report.json",
    ):
        assert os.stat(workspace / "cron" / filename).st_mode & 0o777 == 0o600


def test_user_runtime_maintenance_action_exposes_wake_gate(tmp_path):
    workspace = _workspace(tmp_path)
    runtime = SimpleNamespace(hermes_home=workspace.parent.parent)

    result = _continuity_context("continuity-compaction", runtime, None)

    assert result["wakeAgent"] is True
    assert result["kind"] == "state_compaction"
