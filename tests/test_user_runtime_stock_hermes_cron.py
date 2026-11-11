"""First-message scheduling against stock Hermes 0.23.0: no ``cron ... --json``."""

from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from relic.user_runtime import (
    FIRST_MESSAGE_JOB_NAME,
    RuntimeActivationError,
    RuntimeActivator,
)

from tests.test_user_runtime_activation import (
    ManagedCronFactory,
    ManagedCronSpy,
    _flags,
    _job,
    _positionals,
    _save_state,
    _state,
)


class StockHermesRunner:
    """Mimics stock Hermes: no ``--json``, ``create`` prints ``Created job: <id>``."""

    def __init__(
        self,
        jobs_path: Path,
        *,
        ansi: bool = False,
        print_id: bool = True,
        jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.jobs_path = jobs_path
        self.scripts_dir = jobs_path.parent.parent / "scripts"
        self.ansi = ansi
        self.print_id = print_id
        self.jobs: list[dict[str, Any]] = jobs or []
        self.calls: list[tuple[str, ...]] = []
        self.next_id = 1
        if jobs:
            self._persist()

    def _persist(self) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_path.write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")

    def __call__(self, command, *, env):
        command = tuple(command)
        self.calls.append(command)
        assert command[:3] == (sys.executable, "-m", "hermes_cli.main")
        args = command[3:]
        if "--json" in args:
            return SimpleNamespace(
                returncode=2, stdout="", stderr="unrecognized arguments: --json"
            )
        if args[:2] == ("cron", "create"):
            values = _flags(args[2:])
            positionals = _positionals(args[2:])
            script = values["--script"]
            scripts_dir = self.scripts_dir
            if "/" in script or " " in script or not (scripts_dir / script).is_file():
                return SimpleNamespace(
                    returncode=1,
                    stdout="Failed to create job: Script path must be relative to "
                    f"{scripts_dir}/.",
                    stderr="",
                )
            job = {
                "id": f"abc{self.next_id}23",
                "name": values["--name"],
                "schedule": {"kind": "once", "display": "once soon"},
                "prompt": positionals[1],
                "deliver": values["--deliver"],
                "repeat": {"times": int(values["--repeat"]), "completed": 0},
                "script": script,
            }
            self.next_id += 1
            self.jobs.append(job)
            self._persist()
            if not self.print_id:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            line = f"Created job: {job['id']}"
            if self.ansi:
                line = f"\x1b[32mCreated job: {job['id']}\x1b[0m"
            return SimpleNamespace(returncode=0, stdout=line + "\n", stderr="")
        if args[:2] == ("cron", "edit"):
            job = _job(self.jobs, args[2])
            values = _flags(args[3:])
            job.update(
                schedule=values["--schedule"],
                prompt=values["--prompt"],
                name=values["--name"],
                deliver=values["--deliver"],
                repeat=int(values["--repeat"]),
                script=values["--script"],
            )
            self._persist()
            return SimpleNamespace(returncode=0, stdout="Updated job\n", stderr="")
        if args[:2] == ("cron", "remove"):
            self.jobs.remove(_job(self.jobs, args[2]))
            self._persist()
            return SimpleNamespace(returncode=0, stdout="Removed job\n", stderr="")
        raise AssertionError(args)


def _activator(tmp_path: Path, runner: StockHermesRunner) -> RuntimeActivator:
    return RuntimeActivator(
        runner=runner,
        relic_home=tmp_path,
        hermes_base_home=tmp_path / "hermes",
        environ={},
        managed_cron_factory=ManagedCronFactory(ManagedCronSpy()),
    )


def _jobs_path(tmp_path: Path) -> Path:
    return tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME / "cron" / "jobs.json"


def _owned(runner: StockHermesRunner) -> list[dict[str, Any]]:
    return [j for j in runner.jobs if j["name"] == FIRST_MESSAGE_JOB_NAME]


@pytest.mark.parametrize("ansi", [False, True])
def test_first_message_is_created_on_stock_hermes(tmp_path: Path, ansi: bool) -> None:
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first"),
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    runner = StockHermesRunner(_jobs_path(tmp_path), ansi=ansi)

    activation = _activator(tmp_path, runner).reconcile()

    assert activation.first_message_scheduled is True
    assert len(_owned(runner)) == 1
    assert not any("--json" in call for call in runner.calls)


def test_repeated_reconcile_does_not_duplicate(tmp_path: Path) -> None:
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first"),
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    runner = StockHermesRunner(_jobs_path(tmp_path))

    _activator(tmp_path, runner).reconcile()
    _activator(tmp_path, runner).reconcile()

    assert len(_owned(runner)) == 1


def test_not_requested_removes_existing_job(tmp_path: Path) -> None:
    _save_state(
        tmp_path, _state(), hermes_profiles_home=tmp_path / "hermes" / "profiles"
    )
    existing = {
        "id": "old123",
        "name": FIRST_MESSAGE_JOB_NAME,
        "schedule": "1m",
        "prompt": "p",
        "deliver": "telegram:123456789",
        "repeat": 1,
        "script": "s",
    }
    runner = StockHermesRunner(_jobs_path(tmp_path), jobs=[existing])

    activation = _activator(tmp_path, runner).reconcile()

    assert activation.first_message_scheduled is False
    assert _owned(runner) == []


def test_job_id_is_found_by_name_when_create_prints_none(tmp_path: Path) -> None:
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first"),
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    runner = StockHermesRunner(_jobs_path(tmp_path), print_id=False)

    activation = _activator(tmp_path, runner).reconcile()

    assert activation.first_message_scheduled is True
    assert len(_owned(runner)) == 1


def test_activation_error_keeps_the_sanitized_cause(tmp_path: Path) -> None:
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first"),
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    runner = StockHermesRunner(_jobs_path(tmp_path), print_id=False)
    runner.jobs_path = tmp_path / "unwritable" / "jobs.json"  # create leaves no trace

    with pytest.raises(RuntimeActivationError) as caught:
        _activator(tmp_path, runner).reconcile()
    assert str(caught.value) == "cron activation failed: cron create returned no job id"


def test_symlinked_jobs_file_is_rejected(tmp_path: Path) -> None:
    _save_state(
        tmp_path,
        _state(first_message="gumi_sends_first"),
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    path = _jobs_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text('{"jobs": []}', encoding="utf-8")
    path.symlink_to(target)
    runner = StockHermesRunner(path)

    with pytest.raises(RuntimeActivationError):
        _activator(tmp_path, runner).reconcile()
    assert runner.jobs == []
    assert runner.calls == []  # rejected before any cron command ran
