from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import stat
from datetime import datetime, timezone

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from relic.hermes_plugin.managed_cron import (
    MANAGED_CRON_SPECS,
    ManagedCron,
    ManagedCronError,
)


DELIVERY_NAMES = {
    "gumi_checkin_message",
    "gumi_diegetic_dispatch",
    "gumi_diegetic_message",
    "gumi_proactive_message",
}
EXPECTED_NAMES = (
    "gumi_memory_sync",
    "gumi_checkin_gate",
    "gumi_checkin_message",
    "gumi_diegetic_dispatch",
    "gumi_diegetic_gate",
    "gumi_diegetic_message",
    "gumi_proactive_gate",
    "gumi_proactive_message",
    "gumi_state_compaction",
    "gumi_candidate_review",
    "gumi_memory_exposure_rollup",
    "gumi_provider_eval_rollup",
)

EXPECTED_ACTIONS = {
    "gumi_memory_sync": "continuity-sync",
    "gumi_checkin_gate": "checkin-gate",
    "gumi_checkin_message": "checkin-message",
    "gumi_diegetic_dispatch": "continuity-dispatch",
    "gumi_diegetic_gate": "diegetic-gate",
    "gumi_diegetic_message": "diegetic-message",
    "gumi_proactive_gate": "proactive-gate",
    "gumi_proactive_message": "proactive-message",
    "gumi_state_compaction": "continuity-compaction",
    "gumi_candidate_review": "continuity-review",
    "gumi_memory_exposure_rollup": "memory-exposure-rollup",
    "gumi_provider_eval_rollup": "provider-eval-rollup",
}


class CronRunner:
    def __init__(self, jobs: list[dict[str, object]] | None = None, *, fail_name: str | None = None):
        self.jobs = jobs or []
        self.calls: list[tuple[str, ...]] = []
        self.fail_name = fail_name
        self.next_id = 1

    def __call__(self, command: tuple[str, ...]):
        args = tuple(command)
        self.calls.append(args)
        if args == ("cron", "list", "--json"):
            return _result({"jobs": self.jobs})
        if args[:2] == ("cron", "create"):
            values = _flags(args[2:])
            name = values["--name"]
            if name == self.fail_name:
                return SimpleNamespace(returncode=1, stdout="", stderr="injected failure")
            positionals = _positionals(args[2:])
            job = {
                "id": f"job-{self.next_id}",
                "name": name,
                "schedule": positionals[0],
                "prompt": positionals[1] if len(positionals) > 1 else "",
                "script": values.get("--script", ""),
                "deliver": values.get("--deliver", ""),
                "repeat": int(values.get("--repeat", "0")),
                "no_agent": "--no-agent" in args,
                "enabled_toolsets": ["none"],
            }
            self.next_id += 1
            self.jobs.append(job)
            return _result({"job": job})
        if args[:2] == ("cron", "edit"):
            job = _job(self.jobs, args[2])
            values = _flags(args[3:])
            job.update(
                {
                    "name": values["--name"],
                    "schedule": values["--schedule"],
                    "prompt": values.get("--prompt", ""),
                    "script": values.get("--script", ""),
                    "deliver": values.get("--deliver", ""),
                    "repeat": int(values.get("--repeat", "0")),
                }
            )
            return _result({"job": job})
        if args[:2] == ("cron", "remove"):
            job = _job(self.jobs, args[2])
            self.jobs.remove(job)
            return _result({"removed_job": job})
        raise AssertionError(args)


def _result(value: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")


def _flags(args: tuple[str, ...]) -> dict[str, str]:
    flags_with_values = {"--name", "--deliver", "--repeat", "--script", "--schedule", "--prompt"}
    return {
        argument: args[index + 1]
        for index, argument in enumerate(args)
        if argument in flags_with_values
    }


def _positionals(args: tuple[str, ...]) -> list[str]:
    flags_with_values = {"--name", "--deliver", "--repeat", "--script", "--schedule", "--prompt"}
    values: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in flags_with_values:
            index += 2
        elif args[index] in {"--no-agent", "--json"}:
            index += 1
        else:
            values.append(args[index])
            index += 1
    return values


def _job(jobs: list[dict[str, object]], job_id: str) -> dict[str, object]:
    return next(job for job in jobs if job["id"] == job_id)


def _manager(home: Path, runner: CronRunner, *, specs=MANAGED_CRON_SPECS) -> ManagedCron:
    return ManagedCron(base_home=home, runner=runner, specs=specs)


def test_specs_have_the_twelve_approved_names_and_legacy_schedules() -> None:
    by_name = {spec.name: spec for spec in MANAGED_CRON_SPECS}

    assert tuple(by_name) == EXPECTED_NAMES
    assert by_name["gumi_memory_sync"].schedule == "2-59/30 * * * *"
    assert by_name["gumi_checkin_gate"].schedule == "*/30 * * * *"
    assert by_name["gumi_checkin_message"].schedule == "*/30 * * * *"
    assert by_name["gumi_diegetic_dispatch"].schedule == "17 * * * *"
    assert by_name["gumi_diegetic_gate"].schedule == "20 * * * *"
    assert by_name["gumi_diegetic_message"].schedule == "20 * * * *"
    assert by_name["gumi_proactive_gate"].schedule == "45 * * * *"
    assert by_name["gumi_proactive_message"].schedule == "45 * * * *"
    assert by_name["gumi_state_compaction"].schedule == "0 3 * * *"
    assert by_name["gumi_candidate_review"].schedule == "0 4 * * *"
    assert by_name["gumi_memory_exposure_rollup"].schedule == "0 5 * * 0"
    assert by_name["gumi_provider_eval_rollup"].schedule == "0 6 * * 0"
    assert {spec.name for spec in MANAGED_CRON_SPECS if spec.delivery_dependent} == DELIVERY_NAMES


def test_relic_owned_message_jobs_do_not_auto_deliver() -> None:
    message_jobs = {
        "gumi_checkin_message",
        "gumi_diegetic_message",
        "gumi_proactive_message",
    }

    by_name = {spec.name: spec for spec in MANAGED_CRON_SPECS}

    assert {by_name[name].delivery_target for name in message_jobs} == {"local"}


def test_managed_jobs_are_toolless() -> None:
    assert all(spec.enabled_toolsets == ("none",) for spec in MANAGED_CRON_SPECS)


def test_toolset_persistence_is_verified(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    cron = hermes_home / "cron"
    cron.mkdir(parents=True)
    jobs_path = cron / "jobs.json"
    jobs_path.write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "gumi_diegetic_message"}]}),
        encoding="utf-8",
    )
    manager = _manager(tmp_path, CronRunner())
    manager.configure_hermes_jobs_path(hermes_home)

    manager._patch_toolsets("job-1", ("none",))

    assert json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"][0][
        "enabled_toolsets"
    ] == ["none"]
    jobs_path.unlink()
    with pytest.raises(ManagedCronError, match="unavailable"):
        manager._patch_toolsets("job-1", ("none",))


def test_diegetic_prompt_renders_non_empty_text_without_raising() -> None:
    from relic.hermes_plugin.managed_cron import _diegetic_prompt

    prompt = _diegetic_prompt()

    assert isinstance(prompt, str)
    assert prompt.strip()


def test_scripts_can_use_a_separate_hermes_scripts_directory(tmp_path: Path) -> None:
    runner = CronRunner()
    scripts_dir = tmp_path / "hermes" / "scripts"

    ManagedCron(
        base_home=tmp_path / "relic",
        runner=runner,
        scripts_dir=scripts_dir,
    ).reconcile(delivery_armed=True)

    assert scripts_dir.is_dir()
    assert all(
        isinstance(job.get("script"), str)
        and (scripts_dir / job["script"]).is_file()
        for job in runner.jobs
        if job.get("script")
    )



def test_fresh_reconciliation_creates_every_spec_and_private_manifest(tmp_path: Path) -> None:
    runner = CronRunner()

    result = _manager(tmp_path, runner).reconcile(delivery_armed=True)

    assert result.created_ids == tuple(f"job-{index}" for index in range(1, 13))
    assert {job["name"] for job in runner.jobs} == set(EXPECTED_NAMES)
    assert result.actions == tuple(f"created:{name}" for name in EXPECTED_NAMES)
    manifest = tmp_path / "user" / "managed_cron.json"
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700
    assert set(json.loads(manifest.read_text(encoding="utf-8"))["jobs"]) == set(EXPECTED_NAMES)


def test_rerun_is_a_noop_without_duplicates(tmp_path: Path) -> None:
    runner = CronRunner()
    manager = _manager(tmp_path, runner)
    manager.reconcile(delivery_armed=True)
    calls_before = len(runner.calls)

    result = manager.reconcile(delivery_armed=True)

    assert result.created_ids == ()
    assert result.actions == tuple(f"unchanged:{name}" for name in EXPECTED_NAMES)
    assert len(runner.calls) == calls_before + 1
    assert len(runner.jobs) == len(EXPECTED_NAMES)


def test_rerun_accepts_real_hermes_schedule_and_repeat_shape(tmp_path: Path) -> None:
    runner = CronRunner()
    manager = _manager(tmp_path, runner)
    manager.reconcile(delivery_armed=True)
    for job in runner.jobs:
        schedule = job["schedule"]
        job["schedule"] = {"kind": "cron", "expr": schedule, "display": schedule}
        job["repeat"] = {"times": None, "completed": 7}
    calls_before = len(runner.calls)

    result = manager.reconcile(delivery_armed=True)

    assert result.actions == tuple(f"unchanged:{name}" for name in EXPECTED_NAMES)
    assert len(runner.calls) == calls_before + 1


def test_changed_owned_spec_updates_in_place(tmp_path: Path) -> None:
    runner = CronRunner()
    _manager(tmp_path, runner).reconcile(delivery_armed=True)
    changed = tuple(
        replace(spec, schedule="3-59/30 * * * *") if spec.name == "gumi_memory_sync" else spec
        for spec in MANAGED_CRON_SPECS
    )

    result = _manager(tmp_path, runner, specs=changed).reconcile(delivery_armed=True)

    assert result.actions[0] == "updated:gumi_memory_sync"
    assert result.created_ids == ()
    assert _job(runner.jobs, "job-1")["schedule"] == "3-59/30 * * * *"
    assert ("cron", "edit", "job-1") in {call[:3] for call in runner.calls}


def test_unmanaged_same_name_collision_is_refused_with_remediation(tmp_path: Path) -> None:
    runner = CronRunner(jobs=[{"id": "foreign", "name": "gumi_memory_sync"}])

    with pytest.raises(ManagedCronError, match="gumi_memory_sync.*unmanaged"):
        _manager(tmp_path, runner).reconcile(delivery_armed=False)

    assert runner.jobs == [{"id": "foreign", "name": "gumi_memory_sync"}]
    assert all(call[:2] != ("cron", "remove") for call in runner.calls)


def test_unrelated_jobs_are_preserved_and_commands_never_embed_legacy_ids(tmp_path: Path) -> None:
    runner = CronRunner(jobs=[{"id": "foreign", "name": "other:job"}])

    _manager(tmp_path, runner).reconcile(delivery_armed=True)

    assert _job(runner.jobs, "foreign") == {"id": "foreign", "name": "other:job"}
    joined = " ".join(" ".join(command) for command in runner.calls)
    assert "subject-" not in joined
    assert "experiment-" not in joined


def test_delivery_off_removes_only_owned_delivery_jobs_and_on_recreates_them(tmp_path: Path) -> None:
    runner = CronRunner(jobs=[{"id": "foreign", "name": "other:job"}])
    manager = _manager(tmp_path, runner)
    manager.reconcile(delivery_armed=True)

    off = manager.reconcile(delivery_armed=False)

    assert {action.removeprefix("removed:") for action in off.actions if action.startswith("removed:")} == DELIVERY_NAMES
    assert {job["name"] for job in runner.jobs} == (set(EXPECTED_NAMES) - DELIVERY_NAMES) | {"other:job"}
    on = manager.reconcile(delivery_armed=True)
    assert {action.removeprefix("created:") for action in on.actions if action.startswith("created:")} == DELIVERY_NAMES
    assert len([job for job in runner.jobs if job["name"] == "other:job"]) == 1


def test_partial_create_failure_rolls_back_only_current_transaction_ids(tmp_path: Path) -> None:
    runner = CronRunner(fail_name="gumi_checkin_gate")

    with pytest.raises(ManagedCronError, match="gumi_checkin_gate"):
        _manager(tmp_path, runner).reconcile(delivery_armed=True)

    assert runner.jobs == []
    removals = [command for command in runner.calls if command[:2] == ("cron", "remove")]
    assert removals == [("cron", "remove", "job-1")]

def _assert_no_legacy_system_labels(value: object) -> None:
    forbidden = ("subject", "experiment", "researcher", "consent", "emergency", "risk")
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(label in str(key).lower() for label in forbidden)
            _assert_no_legacy_system_labels(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_legacy_system_labels(item)
    elif isinstance(value, str):
        assert not any(label in value.lower() for label in forbidden)


def test_generated_cron_payloads_are_canonical_user_continuity_only(tmp_path: Path) -> None:
    runner = CronRunner()
    manager = _manager(tmp_path, runner)

    manager.reconcile(delivery_armed=True)

    scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in (tmp_path / "user" / "scripts").iterdir()
    }
    payloads = {
        "command_args": runner.calls,
        "outbound_records": runner.jobs,
        "prompts": {job["name"]: job["prompt"] for job in runner.jobs},
        "scripts": scripts,
        "manifest": json.loads(manager.manifest_path.read_text(encoding="utf-8")),
    }
    _assert_no_legacy_system_labels(payloads)
    assert all("RELIC_RUNTIME_SCOPE_ID" in script for script in scripts.values())
    assert "review consistency and drift across the six canonical gumi files" in next(
        job["prompt"] for job in runner.jobs if job["name"] == "gumi_candidate_review"
    ).lower()


def test_every_managed_job_has_a_private_bounded_action_script(tmp_path: Path) -> None:
    runner = CronRunner()
    _manager(tmp_path, runner).reconcile(delivery_armed=True)

    scripts_dir = tmp_path / "user" / "scripts"
    scripts = {path.name: path.read_text(encoding="utf-8") for path in scripts_dir.iterdir()}

    assert set(scripts) == {f"{name}.sh" for name in EXPECTED_NAMES}
    for name, action in EXPECTED_ACTIONS.items():
        path = scripts_dir / f"{name}.sh"
        content = scripts[path.name]
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert content.startswith("#!/usr/bin/env bash\n")
        assert f'--action "{action}"' in content
        assert "$#" in content
        assert "$RELIC_HOME/user/user_profile.json" in content
        assert "runtime_scope_id" in content
        assert "$RELIC_HOME/user/runtime_scope_id" not in content


def test_canonical_action_interface_is_closed_and_returns_neutral_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from relic.gumi_plugin import memory_sync
    from relic.profile.registry import ProfileRegistry
    from relic.profile.user_runtime import (
        CANONICAL_ACTIONS,
        CanonicalActionError,
        dispatch_action,
        main,
        record_delivery_outcome,
        resolve_runtime_scope_id,
    )

    registry = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes-profiles",
    )
    user = registry.initialize_user()
    registry.materialize_user_artifacts(
        user,
        baseline={"timezone": "UTC"},
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {
                "proactivity": {
                    "recurring_messages": "on",
                    "quiet_hours": "22:00-08:00",
                    "maximum_frequency": "1/day",
                }
            },
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": True,
            "delivery_execution_armed": True,
            "first_message": "wait",
            "credential_references": {},
        },
        checkin_state={
            "recurring": {"status": "armed"},
            "first_message": {"status": "waiting"},
        },
        private_env={},
    )
    workspace = registry.hermes_profiles_home / HERMES_USER_PROFILE_NAME / "workspace" / "gumi"
    workspace.mkdir(parents=True, exist_ok=True)
    for name in (
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    ):
        (workspace / name).write_text(name, encoding="utf-8")
    policy_path = tmp_path / "user" / "checkin" / "delivery_policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        '{"consent_for_active_elicitation": true, '
        '"consent_for_passive_extraction": true}\n',
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    managed_manifest = tmp_path / "user" / "managed_cron.json"
    managed_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {
                    "gumi_checkin_message": {
                        "id": "checkinjob01",
                        "fingerprint": "test",
                    },
                    "gumi_proactive_message": {
                        "id": "proactjob001",
                        "fingerprint": "test",
                    },
                    "gumi_diegetic_message": {
                        "id": "diegetic001",
                        "fingerprint": "test",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILES_HOME", str(registry.hermes_profiles_home))
    calls: list[Path] = []
    monkeypatch.setattr(
        memory_sync,
        "sync",
        lambda hermes_home: calls.append(hermes_home)
        or {"scanned": 0, "appended": 0, "skipped": 0, "errors": 0, "done": True},
    )

    assert set(CANONICAL_ACTIONS) == set(EXPECTED_ACTIONS.values())
    assert resolve_runtime_scope_id(relic_home=tmp_path) == user.runtime_scope_id
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    contexts: dict[str, dict[str, str]] = {}
    for action in CANONICAL_ACTIONS:
        contexts[action] = dispatch_action(
            action,
            relic_home=tmp_path,
            runtime_scope_id=user.runtime_scope_id,
            now=now,
        )
        if action == "checkin-message":
            # Simulate the checkin actually reaching the subject (a mere
            # reservation no longer consumes the shared cap/spacing).
            from relic.profile.user_runtime import consume_delivery_reservation

            consume_delivery_reservation(
                registry.hermes_user_home,
                "cron_checkinjob01_20260101_120000",
                relic_home=tmp_path,
            )
            record_delivery_outcome(registry.hermes_user_home, "checkin", now)

    assert calls == [registry.hermes_user_home]
    assert contexts["continuity-sync"]["status"] == "no-change"
    assert contexts["checkin-gate"]["status"] == "eligible"
    assert contexts["proactive-gate"]["status"] == "silent"
    assert contexts["checkin-message"]["status"] == "ready"
    assert contexts["proactive-message"]["status"] == "silent"
    assert contexts["diegetic-gate"]["status"] == "silent"
    assert contexts["diegetic-message"]["status"] == "silent"
    assert all(
        contexts[action]["wakeAgent"] is True
        for action in ("continuity-compaction", "continuity-review")
    )
    quiet = dispatch_action(
        "checkin-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=datetime(2026, 1, 1, 23, tzinfo=timezone.utc),
    )
    assert quiet == {
        "action": "checkin-gate",
        "context": "[SILENT]",
        "runtime_scope_id": user.runtime_scope_id,
        "status": "silent",
        "wakeAgent": False,
    }
    (tmp_path / "user" / "continuity.json").write_text(
        json.dumps(
            {
                "markers": [
                    {
                        "runtime_scope_id": user.runtime_scope_id,
                        "hermes_profile_id": HERMES_USER_PROFILE_NAME,
                        "user_confirmation": True,
                        "created_at": now.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cadence = dispatch_action(
        "proactive-gate",
        relic_home=tmp_path,
        runtime_scope_id=user.runtime_scope_id,
        now=now,
    )
    assert cadence["status"] == "silent"
    _assert_no_legacy_system_labels({"contexts": contexts, "quiet": quiet, "cadence": cadence})
    with pytest.raises(CanonicalActionError, match="unsupported"):
        dispatch_action(
            "extra",
            relic_home=tmp_path,
            runtime_scope_id=user.runtime_scope_id,
            now=now,
        )
    registry.user_profile_path.chmod(0o644)
    with pytest.raises(CanonicalActionError, match="canonical profile"):
        resolve_runtime_scope_id(relic_home=tmp_path)
    assert main(
        [
            "--relic-home",
            str(tmp_path),
            "--runtime-scope-id",
            user.runtime_scope_id,
            "--action",
            "checkin-gate",
        ]
    ) == 1
    _assert_no_legacy_system_labels(capsys.readouterr().err)



def test_new_rollup_canonical_actions_dispatch_without_raising(tmp_path: Path) -> None:
    from relic.profile.registry import ProfileRegistry
    from relic.profile.user_runtime import CANONICAL_ACTIONS, dispatch_action

    assert "memory-exposure-rollup" in CANONICAL_ACTIONS
    assert "provider-eval-rollup" in CANONICAL_ACTIONS

    registry = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes-profiles",
    )
    user = registry.initialize_user()
    registry.materialize_user_artifacts(
        user,
        baseline={"timezone": "UTC"},
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {
                "proactivity": {
                    "recurring_messages": "on",
                    "quiet_hours": "22:00-08:00",
                    "maximum_frequency": "1/day",
                }
            },
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": True,
            "delivery_execution_armed": True,
            "first_message": "wait",
            "credential_references": {},
        },
        checkin_state={
            "recurring": {"status": "armed"},
            "first_message": {"status": "waiting"},
        },
        private_env={},
    )
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    for action in ("memory-exposure-rollup", "provider-eval-rollup"):
        context = dispatch_action(
            action,
            relic_home=tmp_path,
            runtime_scope_id=user.runtime_scope_id,
            now=now,
        )
        assert context["action"] == action
        assert context["runtime_scope_id"] == user.runtime_scope_id
        assert context["status"] in {"actionable", "no-change"}
        assert context["context"] == "ready"




class StockHermesRunner(CronRunner):
    """Stock Hermes: no `--json` on `cron`, `create` prints only `Created job: <id>`,
    and every change lands in the profile's cron/jobs.json."""

    def __init__(self, jobs_path: Path):
        super().__init__()
        self.jobs_path = jobs_path

    def __call__(self, command: tuple[str, ...]):
        args = tuple(command)
        if "--json" in args:
            return SimpleNamespace(
                returncode=2, stdout="", stderr="hermes: error: unrecognized arguments: --json"
            )
        if args[:2] == ("cron", "list"):
            raise AssertionError("the manager must read jobs.json, not call `cron list`")
        result = super().__call__(args)
        if args[:2] == ("cron", "create"):
            job = self.jobs[-1]
            result = SimpleNamespace(
                returncode=0,
                stdout=f"\x1b[32mCreated job: {job['id']}\x1b[0m\n  Name: {job['name']}\n",
                stderr="",
            )
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_path.write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")
        return result


def test_stock_hermes_without_json_flag_is_managed_through_jobs_file(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    runner = StockHermesRunner(profile / "cron" / "jobs.json")
    manager = _manager(tmp_path, runner)
    manager.configure_hermes_jobs_path(profile)

    manager.reconcile(delivery_armed=False)

    expected = set(EXPECTED_NAMES) - DELIVERY_NAMES  # delivery jobs wait until armed
    created = [call for call in runner.calls if call[:2] == ("cron", "create")]
    assert len(created) == len(expected)
    assert all("--json" not in call for call in created)
    stored = json.loads((profile / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert {job["name"] for job in stored} == expected
    assert all(job["enabled_toolsets"] for job in stored)

    runner.calls.clear()
    manager.reconcile(delivery_armed=False)  # idempotent: nothing to create or edit
    assert [call for call in runner.calls if call[:2] in {("cron", "create"), ("cron", "edit")}] == []


def test_created_job_id_reads_stock_hermes_line_with_colour_codes() -> None:
    from relic.hermes_plugin.managed_cron import _created_job_id

    assert _created_job_id("\x1b[32mCreated job: abc123\x1b[0m\n  Name: x\n") == "abc123"
    assert _created_job_id('{"job": {"id": "j9"}}') == "j9"
    assert _created_job_id("nothing useful") is None
