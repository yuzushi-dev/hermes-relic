"""Manual dryrun exit codes, with model/scheduler I/O replaced at its boundary."""
import json
import sys
from types import ModuleType

import pytest

from relic.profile import manual_cron


@pytest.fixture
def worker(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile"
    relic = tmp_path / "relic"
    (profile / "cron").mkdir(parents=True)
    (relic / "user").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("RELIC_HOME", str(relic))
    monkeypatch.setenv("_RELIC_MANUAL_DRYRUN", "1")
    monkeypatch.setattr(manual_cron, "install_dryrun_guard", lambda: None)
    scheduler = ModuleType("cron.scheduler")
    cron = ModuleType("cron")
    cron.scheduler = scheduler
    monkeypatch.setitem(sys.modules, "cron", cron)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    def run(response, *, action="diegetic-message", success=True, error=None, force=True):
        name = "gumi_" + action.replace("-", "_")
        job = {"id": "test-id", "name": name, "script": name + ".sh",
               "enabled_toolsets": ["none"]}
        (profile / "cron/jobs.json").write_text(json.dumps({"jobs": [job]}))
        (relic / "user/managed_cron.json").write_text(json.dumps({"jobs": {name: {"id": "test-id"}}}))
        scheduler.run_job = lambda job: (success, "", response, error)
        monkeypatch.setenv("_RELIC_MANUAL_FORCE", "1" if force else "0")
        monkeypatch.setattr(sys, "argv", ["manual_cron", "--worker", "--action", action])
        code = manual_cron.main()
        return code, json.loads(capsys.readouterr().out)

    return run


def dispatched(response="[SILENT]", reason="sanitized_empty"):
    return {"dryrun": True, "kind": "diegetic", "response": response,
            "dispatch": {"tipo": "text", "success": False, "reason": reason}}


@pytest.mark.parametrize("action", ["checkin-message", "proactive-message", "diegetic-message"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("success", [False, True])
def test_dryrun_sanitized_silent_is_success(worker, action, force, success):
    detail = dispatched()
    code, report = worker(json.dumps(detail), action=action, force=force, success=success)
    assert code == 0
    assert report["ok"] is True
    assert report["status"] == "silent"
    assert report["dispatch"] == detail["dispatch"]
    assert report["response"] == "[SILENT]"
    assert report["force"] is force


def test_literal_silent_remains_success(worker):
    code, report = worker("[SILENT]")
    assert code == 0
    assert report["ok"] is True
    assert report["status"] == "silent"


@pytest.mark.parametrize("response", ["[SILENT]", json.dumps(dispatched())])
def test_worker_failure_remains_failure(worker, response):
    code, report = worker(response, success=False, error="worker failed")
    assert code == 1
    assert report["ok"] is False
    assert report["error"] == "worker failed"


@pytest.mark.parametrize("response", [
    "invalid json", "[]", "null", '{"dryrun": true}',
    json.dumps(dispatched(reason="policy_denied")),
    json.dumps(dispatched(reason="security_blocked")),
    json.dumps(dispatched(response="text rejected by sanitizer")),
])
def test_invalid_or_unsafe_result_remains_failure(worker, response):
    code, report = worker(response)
    assert code == 1
    assert report["ok"] is False


def test_missing_isolation_remains_failure(worker, monkeypatch):
    def blocked():
        raise PermissionError("isolation unavailable")
    monkeypatch.setattr(manual_cron, "install_dryrun_guard", blocked)
    code, report = worker(json.dumps(dispatched()))
    assert code == 1
    assert report["ok"] is False
    assert "isolation unavailable" in report["error"]


def test_non_dryrun_result_unchanged(worker, monkeypatch):
    monkeypatch.setenv("_RELIC_MANUAL_DRYRUN", "0")
    code, report = worker(json.dumps(dispatched()))
    assert code == 0
    assert report["status"] == "completed"
    assert "dispatch" not in report
