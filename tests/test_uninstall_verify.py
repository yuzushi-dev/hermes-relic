"""Tests for `relic-gumi uninstall` and `relic-gumi verify`."""
from __future__ import annotations
from relic.hermes_plugin.lifecycle import LifecycleStore
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import relic.gumi_install as cli
from relic.hermes_plugin.install import (
    DirectoryPluginChange,
    Installer,
    disable_relic_gumi_plugin,
    remove_directory_plugin,
)
from relic.hermes_plugin.managed_cron import ManagedCron, MANAGED_CRON_SPECS
from relic.profile.registry import ProfileRegistry


# ---------------------------------------------------------------------------
# disable_relic_gumi_plugin / remove_directory_plugin (unit level)
# ---------------------------------------------------------------------------


def test_disable_removes_only_relic_gumi_from_enabled_list(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text(
        "model:\n  default: kept\nplugins:\n  enabled:\n    - other\n    - relic-gumi\n",
        encoding="utf-8",
    )

    change = disable_relic_gumi_plugin(profile_home)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert change.changed is True
    assert config["plugins"]["enabled"] == ["other"]
    assert config["model"]["default"] == "kept"


def test_disable_is_a_noop_when_not_enabled(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("plugins:\n  enabled:\n    - other\n", encoding="utf-8")
    original = config_path.read_text(encoding="utf-8")

    change = disable_relic_gumi_plugin(profile_home)

    assert change.changed is False
    assert config_path.read_text(encoding="utf-8") == original


def test_disable_can_be_rolled_back(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("plugins:\n  enabled:\n    - relic-gumi\n", encoding="utf-8")

    change = disable_relic_gumi_plugin(profile_home)
    change.rollback()

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == ["relic-gumi"]


def test_remove_directory_plugin_removes_existing_copy(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    plugin_dir = profile_home / "plugins" / "relic-gumi"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")

    change = remove_directory_plugin(profile_home)

    assert change.changed is True
    assert not plugin_dir.exists()
    change.commit()
    assert change.backup_path is None or not change.backup_path.exists()


def test_remove_directory_plugin_is_a_noop_when_absent(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)

    change = remove_directory_plugin(profile_home)

    assert change.changed is False


def test_remove_directory_plugin_can_be_rolled_back(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    plugin_dir = profile_home / "plugins" / "relic-gumi"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "marker.txt").write_text("original", encoding="utf-8")

    change = remove_directory_plugin(profile_home)
    change.rollback()

    assert (plugin_dir / "marker.txt").read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# ManagedCron.remove_all / check_owned
# ---------------------------------------------------------------------------


def _owned_manifest(scripts_dir: Path, *names: str) -> dict:
    return {name: {"job_id": f"id-{name}", "fingerprint": "stale"} for name in names}


def _write_manifest(manifest_path: Path, entries: dict[str, tuple[str, str]]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {
                    name: {"id": job_id, "fingerprint": fingerprint}
                    for name, (job_id, fingerprint) in entries.items()
                },
            }
        ),
        encoding="utf-8",
    )


def test_remove_all_deletes_every_owned_job_and_clears_manifest(tmp_path: Path) -> None:
    base_home = tmp_path / ".relic"
    manifest_path = base_home / "user" / "managed_cron.json"
    all_names = [spec.name for spec in MANAGED_CRON_SPECS]
    _write_manifest(manifest_path, {name: (f"id-{name}", "x") for name in all_names})
    removed_ids: list[str] = []

    def runner(command, **_kwargs):
        if tuple(command[:2]) == ("cron", "list"):
            jobs = [{"id": f"id-{name}", "name": name} for name in all_names]
            return 0, json.dumps(jobs), ""
        if tuple(command[:2]) == ("cron", "remove"):
            removed_ids.append(command[2])
            return 0, "", ""
        raise AssertionError(command)

    manager = ManagedCron(base_home=base_home, runner=runner, scripts_dir=tmp_path / "scripts")

    removed = manager.remove_all()

    assert set(removed) == set(all_names)
    assert set(removed_ids) == {f"id-{name}" for name in all_names}
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["jobs"] == {}


def test_remove_all_is_a_noop_on_empty_manifest(tmp_path: Path) -> None:
    base_home = tmp_path / ".relic"

    def runner(command, **_kwargs):
        if tuple(command[:2]) == ("cron", "list"):
            return 0, "[]", ""
        raise AssertionError(command)

    manager = ManagedCron(base_home=base_home, runner=runner, scripts_dir=tmp_path / "scripts")

    assert manager.remove_all() == ()


def test_check_owned_is_read_only(tmp_path: Path) -> None:
    base_home = tmp_path / ".relic"
    manifest_path = base_home / "user" / "managed_cron.json"
    _write_manifest(manifest_path, {})
    scripts_dir = tmp_path / "scripts"

    def runner(command, **_kwargs):
        if tuple(command[:2]) == ("cron", "list"):
            return 0, "[]", ""
        raise AssertionError(f"check_owned must not mutate cron state: {command}")

    manager = ManagedCron(base_home=base_home, runner=runner, scripts_dir=scripts_dir)

    results = manager.check_owned()

    assert all(status.startswith("unowned:") for status in results)
    assert not scripts_dir.exists()  # no script files written as a side effect


def test_check_owned_reports_ok_when_fingerprints_match(tmp_path: Path) -> None:
    from relic.hermes_plugin.managed_cron import _ResolvedSpec

    base_home = tmp_path / ".relic"
    manifest_path = base_home / "user" / "managed_cron.json"
    spec = next(s for s in MANAGED_CRON_SPECS if not s.delivery_dependent)
    script_name = f"{spec.name}.sh" if spec.script is not None else ""
    resolved = _ResolvedSpec(spec, spec.prompt(), script_name)
    fingerprint = resolved.fingerprint()
    _write_manifest(manifest_path, {spec.name: (f"id-{spec.name}", fingerprint)})

    def runner(command, **_kwargs):
        if tuple(command[:2]) == ("cron", "list"):
            job_record = {
                "id": f"id-{spec.name}",
                "name": spec.name,
                "schedule": spec.schedule,
                "prompt": resolved.prompt,
                "script": script_name,
                "no_agent": spec.no_agent,
                "deliver": spec.delivery_target or "",
                "repeat": 0,
                "enabled_toolsets": list(spec.enabled_toolsets),
            }
            return 0, json.dumps([job_record]), ""
        raise AssertionError(command)

    manager = ManagedCron(base_home=base_home, runner=runner, scripts_dir=tmp_path / "scripts")

    results = manager.check_owned()

    assert f"ok:{spec.name}" in results
    assert all(
        status == f"ok:{spec.name}" or status.startswith("unowned:") for status in results
    )


# ---------------------------------------------------------------------------
# Installer.uninstall (integration through the injectable boundary)
# ---------------------------------------------------------------------------


def test_uninstall_removes_cron_plugin_and_config_but_not_user_data(tmp_path: Path) -> None:
    (tmp_path / "hermes").mkdir()
    profile_home = tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME
    plugin_dir = profile_home / "plugins" / "relic-gumi"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (profile_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - relic-gumi\n", encoding="utf-8"
    )
    LifecycleStore(profile_home).claim()  # a real install always leaves the marker
    user_dir = tmp_path / ".relic" / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "user_profile.json").write_text("{}", encoding="utf-8")

    class StubManagedCron:
        def configure_hermes_jobs_path(self, _profile_home):
            pass

        def remove_all(self):
            return ("gumi_memory_sync", "gumi_checkin_gate")

    installer = Installer(
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: StubManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    result = installer.uninstall()

    assert result.plugin_removed is True
    assert result.config_changed is True
    assert result.removed_jobs == ("gumi_memory_sync", "gumi_checkin_gate")
    assert not plugin_dir.exists()
    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == []
    assert (user_dir / "user_profile.json").exists()  # untouched


def test_uninstall_is_idempotent_when_nothing_is_installed(tmp_path: Path) -> None:
    (tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME).mkdir(parents=True)

    class StubManagedCron:
        def configure_hermes_jobs_path(self, _profile_home):
            pass

        def remove_all(self):
            return ()

    installer = Installer(
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: StubManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    result = installer.uninstall()

    assert result.plugin_removed is False
    assert result.config_changed is False
    assert result.removed_jobs == ()


# ---------------------------------------------------------------------------
# CLI: relic-gumi uninstall / relic-gumi verify
# ---------------------------------------------------------------------------


def test_cli_uninstall_requires_confirmation_by_default(monkeypatch, tmp_path, capsys):
    class Installer:
        def uninstall(self, **_kwargs):
            raise AssertionError("must not run without confirmation")

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert cli.main(["uninstall"]) == 1
    assert "aborted" in capsys.readouterr().err


def test_cli_uninstall_yes_skips_confirmation_and_preserves_data(monkeypatch, tmp_path, capsys):
    calls: list[dict[str, object]] = []

    class Installer:
        def uninstall(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                profile_home=tmp_path / "profiles" / HERMES_USER_PROFILE_NAME,
                plugin_removed=True,
                config_changed=True,
                removed_jobs=("gumi_memory_sync",),
            )

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "get_relic_home", lambda: tmp_path / ".relic")

    def _fail_on_prompt(_prompt):
        raise AssertionError("--yes must skip the prompt")

    monkeypatch.setattr("builtins.input", _fail_on_prompt)

    assert cli.main(["uninstall", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "user data preserved" in out
    assert calls == [{"hermes_home": None}]


def test_cli_uninstall_erase_data_requires_typed_confirmation(monkeypatch, tmp_path, capsys):
    class Installer:
        def uninstall(self, **_kwargs):
            return SimpleNamespace(
                profile_home=tmp_path, plugin_removed=True, config_changed=True, removed_jobs=()
            )

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "get_relic_home", lambda: tmp_path / ".relic")
    prompts = iter(["y", "not-erase"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))

    assert cli.main(["uninstall", "--erase-data"]) == 1
    assert "data not erased" in capsys.readouterr().err


def test_cli_uninstall_erase_data_deletes_user_dir_on_confirmation(monkeypatch, tmp_path, capsys):
    user_dir = tmp_path / ".relic" / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "user_profile.json").write_text("{}", encoding="utf-8")

    class Installer:
        def uninstall(self, **_kwargs):
            return SimpleNamespace(
                profile_home=tmp_path, plugin_removed=True, config_changed=True, removed_jobs=()
            )

    monkeypatch.setattr(cli, "Installer", Installer)
    monkeypatch.setattr(cli, "get_relic_home", lambda: tmp_path / ".relic")
    prompts = iter(["y", "erase"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))

    assert cli.main(["uninstall", "--erase-data"]) == 0
    assert not user_dir.exists()
    assert "erased" in capsys.readouterr().out


def test_cli_verify_reports_pass_for_healthy_canonical_state(monkeypatch, tmp_path, capsys):
    relic_home = tmp_path / ".relic"
    user_home = relic_home / "user"
    user_home.mkdir(parents=True)
    user_home.chmod(0o700)
    registry = ProfileRegistry(relic_home=relic_home)
    for path in (
        registry.user_profile_path,
        registry.baseline_response_path,
        registry.gumi_profile_path,
        registry.runtime_policy_path,
        registry.checkin_state_path,
    ):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 0
    out = capsys.readouterr().out
    assert "relic-gumi verify: PASS" in out


def test_cli_verify_fails_on_corrupted_json(monkeypatch, tmp_path, capsys):
    relic_home = tmp_path / ".relic"
    user_home = relic_home / "user"
    user_home.mkdir(parents=True)
    user_home.chmod(0o700)
    registry = ProfileRegistry(relic_home=relic_home)
    for path in (
        registry.user_profile_path,
        registry.baseline_response_path,
        registry.gumi_profile_path,
        registry.runtime_policy_path,
        registry.checkin_state_path,
    ):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    registry.user_profile_path.write_text("{not json", encoding="utf-8")

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 1
    out = capsys.readouterr().out
    assert "relic-gumi verify: FAIL" in out
    assert "not valid JSON" in out


def test_cli_verify_fails_on_wrong_permissions(monkeypatch, tmp_path, capsys):
    relic_home = tmp_path / ".relic"
    user_home = relic_home / "user"
    user_home.mkdir(parents=True)
    user_home.chmod(0o700)
    registry = ProfileRegistry(relic_home=relic_home)
    for path in (
        registry.user_profile_path,
        registry.baseline_response_path,
        registry.gumi_profile_path,
        registry.runtime_policy_path,
        registry.checkin_state_path,
    ):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    registry.baseline_response_path.chmod(0o644)

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 1
    out = capsys.readouterr().out
    assert "relic-gumi verify: FAIL" in out
    assert "expected 0o600" in out


def _prepare_pending_user(tmp_path):
    relic_home = tmp_path / ".relic"
    user_home = relic_home / "user"
    user_home.mkdir(parents=True)
    user_home.chmod(0o700)
    registry = ProfileRegistry(relic_home=relic_home)
    registry.user_profile_path.write_text("{}", encoding="utf-8")
    registry.user_profile_path.chmod(0o600)
    return relic_home, registry


def test_cli_verify_reports_pending_when_telegram_onboarding_never_ran(tmp_path, capsys):
    relic_home, _registry = _prepare_pending_user(tmp_path)

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 0
    out = capsys.readouterr().out
    assert "[PENDING] onboarding not completed on Telegram" in out
    assert "[MISSING]" not in out
    assert "relic-gumi verify: PASS" in out


def test_cli_verify_pending_covers_in_progress_telegram_state(tmp_path, capsys):
    from relic.onboarding.store import ProgressStore

    relic_home, _registry = _prepare_pending_user(tmp_path)
    ProgressStore(relic_home / "user").save({"status": "in_progress", "answers": {}})

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 0
    assert "[PENDING] onboarding not completed on Telegram" in capsys.readouterr().out


def test_cli_verify_warns_on_failed_activation_without_failing(tmp_path, capsys):
    from relic.onboarding.store import ProgressStore

    relic_home, registry = _prepare_pending_user(tmp_path)
    for path in (
        registry.baseline_response_path,
        registry.gumi_profile_path,
        registry.runtime_policy_path,
        registry.checkin_state_path,
    ):
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    ProgressStore(relic_home / "user").save(
        {"status": "complete", "answers": {}, "activation_error": "RuntimeActivationError"}
    )

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 0
    out = capsys.readouterr().out
    assert (
        "[WARN] onboarding saved but scheduling failed (RuntimeActivationError); "
        "send /gumi-setup retry"
    ) in out
    assert "relic-gumi verify: PASS" in out


def test_cli_verify_no_warning_without_activation_error(tmp_path, capsys):
    from relic.onboarding.store import ProgressStore

    relic_home, _registry = _prepare_pending_user(tmp_path)
    ProgressStore(relic_home / "user").save({"status": "complete", "answers": {}})

    cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"])
    assert "[WARN]" not in capsys.readouterr().out


def test_cli_verify_partial_config_is_still_missing_not_pending(tmp_path, capsys):
    relic_home, registry = _prepare_pending_user(tmp_path)
    registry.baseline_response_path.write_text("{}", encoding="utf-8")
    registry.baseline_response_path.chmod(0o600)

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 1
    out = capsys.readouterr().out
    assert "[MISSING]" in out
    assert "[PENDING]" not in out


def test_cli_verify_reports_missing_files(monkeypatch, tmp_path, capsys):
    relic_home = tmp_path / ".relic"
    (relic_home / "user").mkdir(parents=True)
    (relic_home / "user").chmod(0o700)

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 1
    out = capsys.readouterr().out
    assert "[MISSING]" in out
    assert "relic-gumi verify: FAIL" in out


def test_cli_verify_missing_user_home_is_an_error(monkeypatch, tmp_path, capsys):
    relic_home = tmp_path / ".relic"

    assert cli.main(["verify", "--relic-home", str(relic_home), "--skip-cron"]) == 1
    assert "does not exist" in capsys.readouterr().err
