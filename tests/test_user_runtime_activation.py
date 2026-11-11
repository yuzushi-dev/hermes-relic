from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest
from relic.hermes_plugin.install import HermesLocation, Installer

from relic.profile.registry import ProfileRegistry
from relic.user_runtime import (
    FIRST_MESSAGE_JOB_NAME,
    RuntimeActivationError,
    RuntimeActivator,
    main,
)

VALID_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
VALID_TARGET = "123456789"


class CronRunner:
    def __init__(self, jobs: list[dict[str, object]] | None = None, *, fail_name: str | None = None):
        self.jobs = jobs or []
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []
        self.fail_name = fail_name
        self.next_id = 1

    def __call__(self, command, *, env):
        command = tuple(command)
        self.calls.append(command)
        self.environments.append(dict(env))
        assert command[:3] == (sys.executable, "-m", "hermes_cli.main")
        args = command[3:]
        if args[:2] == ("--profile", HERMES_USER_PROFILE_NAME):
            args = args[2:]
        if args in (
            ("cron", "list", "--all"),
            ("cron", "list", "--json"),
            ("cron", "list", "--all", "--json"),
        ):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"jobs": self.jobs}), stderr="")
        if args[:2] == ("cron", "create"):
            values = _flags(args[2:])
            positionals = _positionals(args[2:])
            name = values["--name"]
            if name == self.fail_name:
                return SimpleNamespace(returncode=1, stdout="", stderr="telegram secret failed")
            job = {
                "id": f"job-{self.next_id}",
                "name": name,
                "schedule": positionals[0],
                "prompt": positionals[1] if len(positionals) > 1 else "",
                "deliver": values.get("--deliver", ""),
                "repeat": int(values["--repeat"]),
                "script": values["--script"],
                "no_agent": "--no-agent" in args,
            }
            self.next_id += 1
            self.jobs.append(job)
            self._persist(env)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"job": job}), stderr="")
        if args[:2] == ("cron", "edit"):
            job = _job(self.jobs, args[2])
            values = _flags(args[3:])
            job.update(
                {
                    "schedule": values["--schedule"],
                    "prompt": values["--prompt"],
                    "name": values["--name"],
                    "deliver": values.get("--deliver", ""),
                    "repeat": int(values["--repeat"]),
                    "script": values["--script"],
                    "no_agent": "--no-agent" in args,
                }
            )
            self._persist(env)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"job": job}), stderr="")
        if args[:2] == ("cron", "remove"):
            job = _job(self.jobs, args[2])
            self.jobs.remove(job)
            self._persist(env)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"removed_job": job}), stderr="")
        raise AssertionError(args)

    def _persist(self, env: Mapping[str, str]) -> None:
        profiles_home = env.get("HERMES_PROFILES_HOME")
        if profiles_home:
            path = Path(profiles_home) / HERMES_USER_PROFILE_NAME / "cron" / "jobs.json"
        else:
            hermes_home = env.get("HERMES_HOME")
            if not hermes_home:
                return
            path = Path(hermes_home) / "profiles" / HERMES_USER_PROFILE_NAME / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")


class ManagedCronSpy:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.delivery_armed: list[bool] = []
        self.failure = failure

    def reconcile(self, *, delivery_armed: bool) -> None:
        self.delivery_armed.append(delivery_armed)
        if self.failure is not None:
            raise self.failure


class ManagedCronFactory:
    def __init__(self, manager: ManagedCronSpy) -> None:
        self.manager = manager
        self.calls: list[tuple[Path, Callable[[tuple[str, ...]], Any]]] = []

    def __call__(self, base_home: Path, runner: Callable[[tuple[str, ...]], Any]) -> ManagedCronSpy:
        self.calls.append((base_home, runner))
        return self.manager


def _flags(args: tuple[str, ...]) -> dict[str, str]:
    names = {"--name", "--deliver", "--repeat", "--script", "--schedule", "--prompt"}
    return {
        argument: args[index + 1]
        for index, argument in enumerate(args[:-1])
        if argument in names
    }


def _positionals(args: tuple[str, ...]) -> list[str]:
    names = {"--name", "--deliver", "--repeat", "--script", "--schedule", "--prompt"}
    values: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in names:
            index += 2
        elif args[index] == "--no-agent":
            index += 1
        else:
            values.append(args[index])
            index += 1
    return values


def _job(jobs: list[dict[str, object]], job_id: str) -> dict[str, object]:
    return next(job for job in jobs if job["id"] == job_id)


def _credential_references(values: Mapping[str, str]) -> dict[str, dict[str, str]]:
    return {
        name: {
            "location": "hermes_profile:.env",
            "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
        for name, value in values.items()
        if name in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"}
    }


def _state(
    *, capability: bool = True, armed: bool = False, first_message: str = "wait"
) -> dict[str, Any]:
    private_env = {
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "TELEGRAM_HOME_CHANNEL": VALID_TARGET,
    }
    return {
        "baseline": {},
        "gumi_profile": {
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {},
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        "runtime_policy": {
            "delivery_capability_enabled": capability,
            "delivery_execution_armed": armed,
            "first_message": first_message,
            "credential_references": _credential_references(private_env),
        },
        "checkin_state": {
            "recurring": {"status": "armed" if armed else "off"},
            "first_message": {
                "status": "requested" if first_message == "gumi_sends_first" else "waiting"
            },
        },
        "private_env": private_env,
    }


def _save_state(
    home: Path, state: dict[str, Any], *, hermes_profiles_home: Path | None = None
) -> None:
    registry = ProfileRegistry(
        relic_home=home,
        hermes_profiles_home=hermes_profiles_home or home / "hermes" / "profiles",
    )
    user = registry.initialize_user()
    registry.materialize_user_artifacts(
        user,
        baseline=state["baseline"],
        gumi_profile=state["gumi_profile"],
        runtime_policy=state["runtime_policy"],
        checkin_state=state["checkin_state"],
        private_env=state["private_env"],
    )


def _activator(
    home: Path, runner: CronRunner, managed_cron_factory: ManagedCronFactory
) -> RuntimeActivator:
    return RuntimeActivator(
        runner=runner,
        environ={
            "RELIC_HOME": str(home),
            "HERMES_PROFILES_HOME": str(home / "hermes" / "profiles"),
        },
        managed_cron_factory=managed_cron_factory,
    )


def _owned(runner: CronRunner, name: str) -> list[dict[str, object]]:
    return [job for job in runner.jobs if job.get("name") == name]


def _legacy_checkin_seen(runner: CronRunner) -> bool:
    return "relic-gumi:check-in" in " ".join(" ".join(call) for call in runner.calls)


def test_activation_delegates_canonical_arming_to_managed_cron(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(armed=True))
    runner = CronRunner()
    manager = ManagedCronSpy()
    factory = ManagedCronFactory(manager)

    activation = _activator(tmp_path, runner, factory).reconcile()

    assert activation.recurring_active is True
    assert activation.first_message_scheduled is False
    assert manager.delivery_armed == [True]
    assert len(factory.calls) == 1
    base_home, managed_runner = factory.calls[0]
    assert base_home == tmp_path
    managed_runner(("cron", "list", "--json"))
    assert runner.calls[-1] == (
        sys.executable,
        "-m",
        "hermes_cli.main",
        "cron",
        "list",
        "--json",
    )
    assert runner.jobs == []
    assert not _legacy_checkin_seen(runner)


def test_activation_requires_matching_private_telegram_credential_references(
    tmp_path: Path,
) -> None:
    state = _state(armed=True, first_message="gumi_sends_first")
    state["runtime_policy"]["credential_references"]["TELEGRAM_BOT_TOKEN"]["fingerprint"] = (
        "0" * 64
    )
    _save_state(tmp_path, state)
    runner = CronRunner()
    manager = ManagedCronSpy()

    activation = _activator(tmp_path, runner, ManagedCronFactory(manager)).reconcile()

    assert activation.recurring_active is False
    assert activation.first_message_scheduled is False
    assert manager.delivery_armed == [False]
    assert runner.jobs == []
    configuration = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    ).load_user_configuration()
    assert configuration is not None
    public_state = json.dumps(configuration)
    assert VALID_TOKEN not in public_state
    assert VALID_TARGET not in public_state


def test_activation_requires_valid_private_telegram_values(tmp_path: Path) -> None:
    state = _state(armed=True, first_message="gumi_sends_first")
    state["private_env"]["TELEGRAM_BOT_TOKEN"] = "not-a-telegram-token"
    state["runtime_policy"]["credential_references"] = _credential_references(state["private_env"])
    _save_state(tmp_path, state)
    runner = CronRunner()
    manager = ManagedCronSpy()

    activation = _activator(tmp_path, runner, ManagedCronFactory(manager)).reconcile()

    assert activation.recurring_active is False
    assert activation.first_message_scheduled is False
    assert manager.delivery_armed == [False]
    assert runner.jobs == []


def test_revocation_is_delegated_to_managed_cron_and_preserves_foreign_jobs(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(armed=True))
    runner = CronRunner(jobs=[{"id": "foreign", "name": "other:job"}])
    manager = ManagedCronSpy()
    factory = ManagedCronFactory(manager)
    activator = _activator(tmp_path, runner, factory)

    activator.reconcile()
    _save_state(tmp_path, _state(capability=False, armed=True))
    activation = activator.reconcile()

    assert activation.recurring_active is False
    assert manager.delivery_armed == [True, False]
    assert _job(runner.jobs, "foreign") == {"id": "foreign", "name": "other:job"}
    assert not _legacy_checkin_seen(runner)


def test_first_message_is_one_shot_without_recurring_arming_and_is_idempotent(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(first_message="gumi_sends_first"))
    runner = CronRunner()
    manager = ManagedCronSpy()
    factory = ManagedCronFactory(manager)
    activator = _activator(tmp_path, runner, factory)

    first = activator.reconcile()
    second = activator.reconcile()

    assert first.first_message_scheduled is True
    assert second.first_message_scheduled is True
    assert manager.delivery_armed == [False, False]
    jobs = _owned(runner, FIRST_MESSAGE_JOB_NAME)
    assert len(jobs) == 1
    assert jobs[0]["repeat"] == 1
    assert VALID_TOKEN not in " ".join(runner.calls[-1])
    assert not _legacy_checkin_seen(runner)


def test_first_message_reconciliation_repairs_duplicates_and_preserves_foreign_jobs(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(first_message="gumi_sends_first"))
    runner = CronRunner(jobs=[{"id": "foreign", "name": "other:job"}])
    manager = ManagedCronSpy()
    factory = ManagedCronFactory(manager)
    activator = _activator(tmp_path, runner, factory)
    activator.reconcile()
    duplicate = deepcopy(_owned(runner, FIRST_MESSAGE_JOB_NAME)[0])
    duplicate["id"] = "duplicate"
    runner.jobs.append(duplicate)
    runner._persist(runner.environments[-1])  # stock Hermes: jobs are read from jobs.json

    activator.reconcile()

    assert len(_owned(runner, FIRST_MESSAGE_JOB_NAME)) == 1
    assert _job(runner.jobs, "foreign") == {"id": "foreign", "name": "other:job"}
    assert manager.delivery_armed == [False, False]
    assert not _legacy_checkin_seen(runner)


def test_managed_cron_failure_prevents_first_message_reconciliation(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(armed=True, first_message="gumi_sends_first"))
    runner = CronRunner()
    manager = ManagedCronSpy(failure=RuntimeError("managed cron failed"))
    factory = ManagedCronFactory(manager)

    with pytest.raises(RuntimeActivationError, match="cron activation failed"):
        _activator(tmp_path, runner, factory).reconcile()

    assert manager.delivery_armed == [True]
    assert runner.jobs == []
    assert all(call[5:7] != ("cron", "create") for call in runner.calls)


def test_first_message_failure_does_not_roll_back_managed_cron_jobs(tmp_path: Path) -> None:
    _save_state(tmp_path, _state(armed=True, first_message="gumi_sends_first"))
    managed_job = {"id": "managed", "name": "gumi_checkin_message"}
    runner = CronRunner(jobs=[managed_job], fail_name=FIRST_MESSAGE_JOB_NAME)
    manager = ManagedCronSpy()
    factory = ManagedCronFactory(manager)

    with pytest.raises(RuntimeActivationError, match="cron activation failed"):
        _activator(tmp_path, runner, factory).reconcile()

    assert manager.delivery_armed == [True]
    assert runner.jobs == [managed_job]
    assert all(call[5:7] != ("cron", "remove") for call in runner.calls)
    assert not _legacy_checkin_seen(runner)


def test_gate_reports_only_armed_or_blocked_without_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    _save_state(tmp_path, _state(armed=True))
    monkeypatch.setenv("RELIC_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILES_HOME", str(tmp_path / "hermes" / "profiles"))

    assert main(["gate"]) == 0
    assert capsys.readouterr().out == "ARMED\n"

    _save_state(tmp_path, _state())
    assert main(["gate"]) == 0
    output = capsys.readouterr().out
    assert output == "BLOCKED\n"
    assert VALID_TOKEN not in output


def test_custom_hermes_activation_reuses_canonical_relic_cron_manifest(
    tmp_path: Path,
) -> None:
    relic_home = tmp_path / "relic"
    hermes_home = tmp_path / "hermes"
    _save_state(
        relic_home,
        _state(armed=True),
        hermes_profiles_home=hermes_home / "profiles",
    )
    runner = CronRunner()
    location = HermesLocation(
        base_home=hermes_home,
        profile_home=hermes_home / "profiles" / HERMES_USER_PROFILE_NAME,
        source="test",
    )
    Installer(
        runner=runner,
        environ={"RELIC_HOME": str(relic_home), "HERMES_HOME": str(hermes_home)},
        home=tmp_path,
    )._reconcile_managed_cron(location)

    manifest = relic_home / "user" / "managed_cron.json"
    calls_before_activation = len(runner.calls)
    activation = RuntimeActivator(
        runner=runner,
        environ={
            "RELIC_HOME": str(relic_home),
            "HERMES_HOME": str(tmp_path / "wrong-hermes"),
        },
        hermes_base_home=hermes_home,
    ).reconcile()

    assert activation.recurring_active is True
    assert manifest.is_file()
    assert not (hermes_home / "user" / "managed_cron.json").exists()
    assert len(runner.jobs) == 12
    assert {
        environment["HERMES_HOME"]
        for environment in runner.environments[calls_before_activation:]
    } == {str(hermes_home / "profiles" / HERMES_USER_PROFILE_NAME)}
