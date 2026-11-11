"""First-message job against the real shape of stock Hermes 0.23.0.

Stock ``cron create`` only accepts ``--script`` as the NAME of a file that exists
in ``<profile>/scripts/``, and stores ``schedule``/``repeat`` as dicts.
"""

from __future__ import annotations

import pytest
from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relic.user_runtime import (
    FIRST_MESSAGE_JOB_NAME,
    RuntimeActivationError,
    RuntimeActivator,
    _first_message_spec,
)

from tests.test_user_runtime_activation import (
    ManagedCronFactory,
    ManagedCronSpy,
    _flags,
    _positionals,
    _save_state,
    _state,
)

SCRIPT_NAME = "relic-gumi-first-message.sh"


class StockShapeRunner:
    """Emulates stock Hermes: validates ``--script`` and stores dict-shaped records."""

    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self.jobs_path = profile / "cron" / "jobs.json"
        self.jobs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.next_id = 1

    def writes(self) -> list[tuple[str, ...]]:
        return [c[3:] for c in self.calls if c[3:5] in (("cron", "create"), ("cron", "edit"), ("cron", "remove"))]

    def _persist(self) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_path.write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")

    def _check_script(self, script: str):
        if "/" in script or " " in script or script.startswith("~"):
            return SimpleNamespace(
                returncode=1,
                stdout=(
                    "Failed to create job: Script path must be relative to "
                    f"{self.profile}/scripts/. Got absolute or home-relative path: '{script}'."
                ),
                stderr="",
            )
        if not (self.profile / "scripts" / script).is_file():
            return SimpleNamespace(
                returncode=1, stdout=f"Failed to create job: Script file not found: {script}", stderr=""
            )
        return None

    def __call__(self, command, *, env):
        command = tuple(command)
        self.calls.append(command)
        assert command[:3] == (sys.executable, "-m", "hermes_cli.main")
        args = command[3:]
        if args[:2] == ("cron", "create"):
            values = _flags(args[2:])
            rejected = self._check_script(values["--script"])
            if rejected is not None:
                return rejected
            positionals = _positionals(args[2:])
            job = {
                "id": f"stock{self.next_id:03d}",
                "name": values["--name"],
                "prompt": positionals[1],
                "script": values["--script"],
                "no_agent": False,
                "schedule": {"kind": "once", "run_at": "2026-11-10T10:00:00+00:00", "display": "once at 2026-11-10 10:00"},
                "repeat": {"times": int(values["--repeat"]), "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "deliver": values["--deliver"],
            }
            self.next_id += 1
            self.jobs.append(job)
            self._persist()
            return SimpleNamespace(returncode=0, stdout=f"Created job: {job['id']}\n", stderr="")
        if args[:2] == ("cron", "edit"):
            job = next(j for j in self.jobs if j["id"] == args[2])
            values = _flags(args[3:])
            rejected = self._check_script(values["--script"])
            if rejected is not None:
                return rejected
            job.update(
                name=values["--name"],
                prompt=values["--prompt"],
                script=values["--script"],
                deliver=values["--deliver"],
                schedule={"kind": "once", "run_at": "2026-11-10T10:05:00+00:00", "display": "once at 2026-11-10 10:05"},
                repeat={"times": int(values["--repeat"]), "completed": 0},
            )
            self._persist()
            return SimpleNamespace(returncode=0, stdout="Updated job\n", stderr="")
        if args[:2] == ("cron", "remove"):
            self.jobs = [j for j in self.jobs if j["id"] != args[2]]
            self._persist()
            return SimpleNamespace(returncode=0, stdout="Removed job\n", stderr="")
        raise AssertionError(args)


def _setup(tmp_path: Path, *, first: bool = True, manager: ManagedCronSpy | None = None):
    profiles = tmp_path / "hermes" / "profiles"
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first") if first else _state(),
        hermes_profiles_home=profiles,
    )
    profile = profiles / HERMES_USER_PROFILE_NAME
    runner = StockShapeRunner(profile)

    def activator() -> RuntimeActivator:
        return RuntimeActivator(
            runner=runner,
            relic_home=tmp_path,
            hermes_base_home=tmp_path / "hermes",
            environ={},
            managed_cron_factory=ManagedCronFactory(manager or ManagedCronSpy()),
        )

    return profile, runner, activator


def _owned(runner: StockShapeRunner) -> list[dict[str, Any]]:
    return [j for j in runner.jobs if j["name"] == FIRST_MESSAGE_JOB_NAME]


def test_first_message_runs_a_profile_script_by_name(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)

    activation = activator().reconcile()

    assert activation.first_message_scheduled is True
    (job,) = _owned(runner)
    assert job["script"] == SCRIPT_NAME
    script = profile / "scripts" / SCRIPT_NAME
    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert stat.S_IMODE(script.parent.stat().st_mode) == 0o700
    text = script.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "exit 64" in text
    assert 'RELIC_HOME="${RELIC_HOME:-$HOME/.relic}"' in text
    assert "relic.user_runtime gate first-message" in text
    assert f"${{RELIC_PYTHON:-{sys.executable}}}" in text


def test_second_reconcile_issues_no_cron_write(tmp_path: Path) -> None:
    _profile, runner, activator = _setup(tmp_path)
    activator().reconcile()
    before = list(_owned(runner)[0].items())
    runner.calls.clear()

    again = activator().reconcile()

    assert again.first_message_scheduled is True
    assert runner.writes() == []
    assert list(_owned(runner)[0].items()) == before  # schedule untouched


def test_not_requested_removes_job_and_script(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)
    activator().reconcile()
    assert (profile / "scripts" / SCRIPT_NAME).exists()
    _save_state(
        tmp_path, _state(), hermes_profiles_home=tmp_path / "hermes" / "profiles"
    )

    activation = activator().reconcile()

    assert activation.first_message_scheduled is False
    assert _owned(runner) == []
    assert not (profile / "scripts" / SCRIPT_NAME).exists()


def test_not_requested_without_script_is_fine(tmp_path: Path) -> None:
    _profile, runner, activator = _setup(tmp_path, first=False)

    assert activator().reconcile().first_message_scheduled is False
    assert runner.writes() == []


def test_changed_script_content_is_rewritten(tmp_path: Path) -> None:
    profile, _runner, activator = _setup(tmp_path)
    activator().reconcile()
    script = profile / "scripts" / SCRIPT_NAME
    script.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")

    activator().reconcile()

    assert "relic.user_runtime gate first-message" in script.read_text(encoding="utf-8")
    assert stat.S_IMODE(script.stat().st_mode) == 0o700


def test_symlinked_scripts_directory_is_rejected(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "scripts").symlink_to(elsewhere)

    with pytest.raises(RuntimeActivationError):
        activator().reconcile()
    assert list(elsewhere.iterdir()) == []
    assert runner.writes() == []


def test_generated_script_rejects_arguments(tmp_path: Path) -> None:
    import subprocess

    profile, _runner, activator = _setup(tmp_path)
    activator().reconcile()
    result = subprocess.run(
        [str(profile / "scripts" / SCRIPT_NAME), "extra"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )
    assert result.returncode == 64
    assert "no arguments" in result.stderr


def test_creation_failure_after_script_write_removes_the_new_script(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)

    def reject(command, *, env):
        runner.calls.append(tuple(command))
        return SimpleNamespace(returncode=1, stdout="Failed to create job: nope", stderr="")

    act = activator()
    act.runner = reject
    with pytest.raises(RuntimeActivationError):
        act.reconcile()

    assert runner.jobs == []
    assert not (profile / "scripts" / SCRIPT_NAME).exists()  # nothing orphaned


def test_rollback_after_later_failure_removes_created_job(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)
    act = activator()
    original = act._reconcile_first_message

    def reconcile_then_fail(spec, changes):
        original(spec, changes)
        raise RuntimeActivationError("late failure")

    act._reconcile_first_message = reconcile_then_fail
    with pytest.raises(RuntimeActivationError):
        act.reconcile()

    assert runner.jobs == []  # created job removed by rollback
    assert not (profile / "scripts" / SCRIPT_NAME).exists()  # script coherent with no job


def test_rollback_of_removal_restores_script_and_recreates_job(tmp_path: Path) -> None:
    profile, runner, activator = _setup(tmp_path)
    activator().reconcile()
    _save_state(tmp_path, _state(), hermes_profiles_home=tmp_path / "hermes" / "profiles")
    act = activator()
    original = act._reconcile_first_message

    def reconcile_then_fail(spec, changes):
        original(spec, changes)
        raise RuntimeActivationError("late failure")

    act._reconcile_first_message = reconcile_then_fail
    with pytest.raises(RuntimeActivationError):
        act.reconcile()

    (job,) = _owned(runner)
    assert job["script"] == SCRIPT_NAME
    assert (profile / "scripts" / SCRIPT_NAME).is_file()


def test_stock_record_shape_parses_to_the_desired_spec(tmp_path: Path) -> None:
    _profile, runner, activator = _setup(tmp_path)
    activator().reconcile()

    spec = _first_message_spec(_owned(runner)[0])

    assert spec is not None
    assert spec.script == SCRIPT_NAME
    assert spec.repeat == 1
    assert spec.deliver == "telegram:123456789"


def test_interval_dict_and_string_schedules_are_both_understood() -> None:
    base = {
        "name": FIRST_MESSAGE_JOB_NAME,
        "prompt": "p",
        "deliver": "telegram:123456789",
        "script": SCRIPT_NAME,
    }
    as_dict = _first_message_spec({**base, "schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"}, "repeat": {"times": 1, "completed": 0}})
    as_str = _first_message_spec({**base, "schedule": "1m", "repeat": 1})
    assert as_dict is not None and as_str is not None
    assert as_dict == as_str


def test_repeat_dict_without_times_is_not_a_spec() -> None:
    job = {
        "name": FIRST_MESSAGE_JOB_NAME,
        "prompt": "p",
        "deliver": "telegram:1",
        "script": SCRIPT_NAME,
        "schedule": "1m",
        "repeat": {"completed": 0},
    }
    assert _first_message_spec(job) is None
