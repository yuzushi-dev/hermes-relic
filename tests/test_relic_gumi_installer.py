from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest
import yaml

from relic.hermes_plugin.install import (
    CommandResult,
    GatewayChange,
    GatewayConflictError,
    GatewayState,
    HermesDiscoveryError,
    HermesLocation,
    Installer,
    InstallerError,
    PluginVerificationError,
    PrerequisiteError,
    _gateway_state,
    discover_hermes_home,
    enable_relic_gumi_plugin,
    gateway_plan,
    install_directory_plugin,
    plugin_artifact_source,
)

from relic.hermes_plugin.lifecycle import LifecycleState, LifecycleStore, outbound_allowed

from relic.hermes_plugin.managed_cron import CronReconciliation


class _NoopManagedCron:
    def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
        return CronReconciliation(actions=(), created_ids=())

    def rollback(self, reconciliation: CronReconciliation) -> None:
        pass


def test_discovery_prefers_explicit_home_over_environment(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    location = discover_hermes_home(
        hermes_home=explicit,
        environ={"HERMES_HOME": str(tmp_path / "environment")},
        home=tmp_path / "home",
    )

    assert location.base_home == explicit.resolve()
    assert location.profile_home == explicit.resolve() / "profiles" / HERMES_USER_PROFILE_NAME
    assert location.source == "argument"


def test_discovery_rejects_missing_explicit_home(tmp_path: Path) -> None:
    with pytest.raises(HermesDiscoveryError, match="does not exist"):
        discover_hermes_home(hermes_home=tmp_path / "missing")

@pytest.mark.parametrize(
    "environ",
    [
        {"HERMES_HOME": "{missing}"},
        {
            "HERMES_HOME": "{missing}",
            "HERMES_PROFILES_HOME": "{missing}/profiles",
        },
        {"HERMES_PROFILES_HOME": "{missing}/profiles"},
    ],
)
def test_discovery_rejects_missing_environment_home(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    missing = tmp_path / "missing"
    configured = {
        name: value.format(missing=missing)
        for name, value in environ.items()
    }

    with pytest.raises(HermesDiscoveryError, match="does not exist"):
        discover_hermes_home(environ=configured, home=tmp_path / "home")


def test_discovery_rejects_conflicting_environment_homes(tmp_path: Path) -> None:
    with pytest.raises(HermesDiscoveryError, match="--hermes-home"):
        discover_hermes_home(
            environ={
                "HERMES_HOME": str(tmp_path / "one"),
                "HERMES_PROFILES_HOME": str(tmp_path / "two" / "profiles"),
            },
            home=tmp_path / "home",
        )


def test_noninteractive_prerequisites_report_exact_remediation(tmp_path: Path) -> None:
    installer = Installer(
        package_available=lambda _module: False,
        which=lambda _name: None,
        environ={},
        home=tmp_path,
    )

    with pytest.raises(PrerequisiteError) as error:
        installer.ensure_prerequisites(non_interactive=True)

    assert str(error.value).startswith("Missing prerequisites: ")
    assert "Remediation: " in str(error.value)
    assert " -m pip install " in str(error.value)


def test_prerequisites_prompt_once_before_one_consolidated_install(tmp_path: Path) -> None:
    prompts: list[str] = []
    commands: list[tuple[str, ...]] = []
    available = {"jsonschema", "pydantic", "yaml", "structlog"}

    def package_available(name: str) -> bool:
        return name in available

    def runner(command, **_kwargs):
        commands.append(tuple(command))
        available.add("hermes_cli")
        return 0, "", ""

    installer = Installer(
        package_available=package_available,
        which=lambda _name: None,
        prompt=lambda question: prompts.append(question) or True,
        runner=runner,
        environ={},
        home=tmp_path,
    )

    installer.ensure_prerequisites()

    assert len(prompts) == 1
    assert len(commands) == 1
    assert commands[0][1:4] == ("-m", "pip", "install")




def test_gateway_commands_use_current_python_installation() -> None:
    assert Installer()._command("gateway", "status")[:4] == [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "--profile",
    ]


def test_prerequisites_fail_before_initializing_user(tmp_path: Path) -> None:
    initialized: list[Path] = []
    (tmp_path / "hermes").mkdir()
    installer = Installer(
        package_available=lambda _module: False,
        initialize_user=lambda profiles_home: initialized.append(profiles_home),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    with pytest.raises(PrerequisiteError):
        installer.install(non_interactive=True)

    assert initialized == []


def test_install_initializes_governance_database(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    relic_home = tmp_path / "relic"
    hermes_home.mkdir()

    def runner(command, **_kwargs):
        if tuple(command[-2:]) in {("cron", "status"), ("gateway", "status")}:
            return 0, "healthy", ""
        return 0, "", ""

    installer = Installer(
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: _NoopManagedCron(),
        environ={
            "HERMES_HOME": str(hermes_home),
            "RELIC_HOME": str(relic_home),
        },
        home=tmp_path,
    )

    installer.install()

    with sqlite3.connect(relic_home / "relic.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "chronicle_events" in tables

def test_packaged_directory_plugin_contains_only_runtime_artifact_files() -> None:
    source = plugin_artifact_source()

    assert (source / "plugin.yaml").is_file()
    assert (source / "__init__.py").is_file()
    assert not (source / "src").exists()




def test_identical_plugin_install_is_a_noop(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME

    first = install_directory_plugin(profile_home)
    first.commit()
    repeated = install_directory_plugin(profile_home)

    assert first.changed is True
    assert repeated.changed is False
    assert repeated.path.is_dir()
    assert not repeated.path.is_symlink()


def test_runtime_change_replaces_identical_entry_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import relic.hermes_plugin.install as install_module

    runtime_root = tmp_path / "runtime" / "relic"
    runtime_file = runtime_root / "hermes_plugin" / "install.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("runtime = 1\n", encoding="utf-8")
    monkeypatch.setattr(install_module, "__file__", str(runtime_file))

    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("name: relic-gumi\n", encoding="utf-8")
    (source / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    profile_home = runtime_root / "generated-profile"

    first = install_directory_plugin(profile_home, source=source)
    first.commit()
    cache = runtime_root / "__pycache__"
    cache.mkdir()
    (cache / "noise.cache").write_text("ignored\n", encoding="utf-8")
    (cache / "install.pyc").write_bytes(b"ignored")
    repeated = install_directory_plugin(profile_home, source=source)
    runtime_file.write_text("runtime = 2\n", encoding="utf-8")
    changed = install_directory_plugin(profile_home, source=source)

    assert (first.path / ".relic-runtime.sha256").is_file()
    assert repeated.changed is False
    assert changed.changed is True


def test_unfingerprintable_existing_plugin_is_replaced(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    target = profile_home / "plugins" / "relic-gumi"
    target.mkdir(parents=True)
    (target / "unexpected-link").symlink_to(tmp_path / "missing")

    change = install_directory_plugin(profile_home)
    change.commit()

    assert change.changed is True
    assert (change.path / "plugin.yaml").is_file()
    assert not (change.path / "unexpected-link").is_symlink()


def test_wheel_metadata_exposes_canonical_installer_and_artifact() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert 'relic-gumi = "relic.gumi_install:main"' in pyproject
    assert '"relic.hermes_plugin.gumi_entry" = ["plugin.yaml"]' in pyproject
    source_root = Path(__file__).resolve().parents[1] / "src" / "relic" / "hermes_plugin"
    assert (source_root / "directory_plugin.py").is_file()
    assert not (source_root / "directory_plugin" / "__init__.py").exists()
    assert not (source_root / "hermes_entry" / "__init__.py").exists()

def test_atomic_plugin_replace_restores_preexisting_artifact_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    target = profile_home / "plugins" / "relic-gumi"
    target.mkdir(parents=True)
    (target / "sentinel").write_text("old", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("name: relic-gumi\n", encoding="utf-8")
    (source / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")

    import relic.hermes_plugin.install as install

    real_replace = install.os.replace

    def fail_stage_replace(source_path, destination):
        if Path(destination) == target and ".stage-" in Path(source_path).name:
            raise OSError("injected failure")
        return real_replace(source_path, destination)

    monkeypatch.setattr(install.os, "replace", fail_stage_replace)

    with pytest.raises(OSError, match="injected failure"):
        install_directory_plugin(profile_home, source=source)

    assert (target / "sentinel").read_text(encoding="utf-8") == "old"


def test_plugin_enable_preserves_unrelated_profile_config(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text(
        "model:\n  default: kept\nplugins:\n  enabled:\n    - other\n  disabled:\n    - relic-gumi\n",
        encoding="utf-8",
    )

    change = enable_relic_gumi_plugin(profile_home)
    content = config_path.read_text(encoding="utf-8")

    assert change.changed is True
    assert "default: kept" in content
    assert "- other" in content
    assert "- relic-gumi" in content
    assert content.count("- relic-gumi") == 1

def test_plugin_enable_defaults_to_clean_cron_delivery(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("model:\n  default: kept\n", encoding="utf-8")

    enable_relic_gumi_plugin(profile_home)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["cron"]["wrap_response"] is False


def test_plugin_enable_preserves_explicit_cron_wrapper_choice(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("cron:\n  wrap_response: true\n", encoding="utf-8")

    enable_relic_gumi_plugin(profile_home)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["cron"]["wrap_response"] is True


def test_plugin_enable_omits_empty_disabled_list(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("model:\n  default: kept\n", encoding="utf-8")

    enable_relic_gumi_plugin(profile_home)

    assert "disabled:" not in config_path.read_text(encoding="utf-8")


def test_plugin_enable_preserves_existing_config_mode(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    profile_home.mkdir(parents=True)
    config_path = profile_home / "config.yaml"
    config_path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    config_path.chmod(0o600)

    enable_relic_gumi_plugin(profile_home)

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_install_removes_legacy_base_home_plugin_and_config(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    legacy_relic_source = tmp_path / "legacy" / "relic" / "hermes_plugin" / "hermes_entry"
    legacy_relic_source.mkdir(parents=True)
    legacy_relic = hermes_home / "plugins" / "relic"
    legacy_relic.parent.mkdir(parents=True)
    legacy_relic.symlink_to(legacy_relic_source, target_is_directory=True)
    legacy_gumi = hermes_home / "plugins" / "relic-gumi"
    (legacy_gumi / "src" / "relic").mkdir(parents=True)
    (legacy_gumi / "plugin.yaml").write_text("name: relic-gumi\n", encoding="utf-8")
    (legacy_gumi / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - other\n"
        "    - relic\n"
        "    - relic-gumi\n"
        "  entries:\n"
        "    other:\n"
        "      preserved: true\n"
        "    relic:\n"
        "      subject_id: old-relic\n"
        "    relic-gumi:\n"
        "      subject_id: legacy\n"
        "      profile_name: legacy\n",
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        if tuple(command[-2:]) in {("cron", "status"), ("gateway", "status")}:
            return 0, "healthy", ""
        return 0, "", ""

    installer = Installer(
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: _NoopManagedCron(),
        environ={"HERMES_HOME": str(hermes_home)},
        home=tmp_path,
    )

    result = installer.install()
    base_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert not legacy_relic.exists()
    assert not legacy_gumi.exists()
    assert base_config["plugins"]["enabled"] == ["other"]
    assert base_config["plugins"]["entries"] == {"other": {"preserved": True}}
    assert (result.profile_home / "plugins" / "relic-gumi").is_dir()



def test_lifecycle_blocks_outbound_until_gateway_ready(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME
    lifecycle = LifecycleStore(profile_home)

    lifecycle.begin()
    for state in (
        LifecycleState.INSTALLED,
        LifecycleState.ENABLED,
        LifecycleState.HOST_V2_LOADED,
        LifecycleState.THREE_HOOKS_VERIFIED,
    ):
        lifecycle.advance(state)

    assert not outbound_allowed(profile_home)
    lifecycle.advance(LifecycleState.GATEWAY_READY)
    assert outbound_allowed(profile_home)
    assert stat.S_IMODE(lifecycle.path.stat().st_mode) == 0o600

    lifecycle.fail("hook verification failed")
    assert not outbound_allowed(profile_home)

@pytest.mark.parametrize(
    ("state", "changed", "expected"),
    [
        (GatewayState.ABSENT, False, ("install", "start")),
        (GatewayState.STOPPED, False, ("start",)),
        (GatewayState.HEALTHY, False, ()),
        (GatewayState.HEALTHY, True, ("restart",)),
        (GatewayState.STALE, False, ("repair", "start")),
    ],
)
def test_gateway_plan_covers_every_safe_lifecycle_state(
    state: GatewayState, changed: bool, expected: tuple[str, ...]
) -> None:
    assert gateway_plan(state, changed=changed) == expected


def test_gateway_conflict_is_a_hard_stop() -> None:
    with pytest.raises(GatewayConflictError):
        gateway_plan(GatewayState.CONFLICT, changed=False)


def test_gateway_state_rejects_mixed_status_output() -> None:
    result = CommandResult(0, "gateway: healthy\nservice: stopped")

    assert _gateway_state(result) is GatewayState.CONFLICT


def test_gateway_state_does_not_treat_nonstatus_failure_text_as_stale() -> None:
    assert _gateway_state(CommandResult(0, "failed: 0")) is GatewayState.HEALTHY


@pytest.mark.parametrize(
    ("output", "expected"),
    (
        (
            "✗ User gateway service is stopped\n"
            "     Active: inactive (dead) since Sun 2026-08-10 10:00:00 CEST",
            GatewayState.STOPPED,
        ),
        (
            "     Active: active (running) since Thu 2026-08-06 23:03:13 CEST\n"
            "✓ User gateway service is running",
            GatewayState.HEALTHY,
        ),
    ),
)
def test_gateway_state_parses_real_hermes_systemd_output(
    output: str, expected: GatewayState
) -> None:
    assert _gateway_state(CommandResult(0, output)) is expected


def test_stale_gateway_rollback_preserves_preexisting_service(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        return 0, "", ""

    installer = Installer(runner=runner)
    location = HermesLocation(
        base_home=tmp_path / "hermes",
        profile_home=tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME,
        source="test",
    )

    installer._rollback_gateway(
        location,
        GatewayChange(previous=GatewayState.STALE, actions=("repair", "start")),
    )

    assert calls == []


def test_cron_status_failure_precedes_gateway_reconciliation(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    (tmp_path / "hermes").mkdir()

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        if tuple(command[-2:]) == ("cron", "status"):
            return 1, "", "cron unavailable"
        return 0, "healthy", ""

    installer = Installer(
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    with pytest.raises(InstallerError, match="Cron check failed"):
        installer.install()

    profile_home = tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME
    assert any(command[-2:] == ("cron", "status") for command in calls)
    assert all("gateway" not in command for command in calls)
    assert LifecycleStore(profile_home).current() is LifecycleState.FAILED
    assert not outbound_allowed(profile_home)


def test_hook_gate_blocks_gateway_and_rolls_back_relic_changes(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    (tmp_path / "hermes").mkdir()

    def runner(command, **_kwargs):
        commands.append(tuple(command))
        return 0, "healthy", ""

    installer = Installer(
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=runner,
        verifier=lambda _profile_home: (_ for _ in ()).throw(RuntimeError("missing hook")),
        cron_checker=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    with pytest.raises(PluginVerificationError, match="missing hook"):
        installer.install()

    profile_home = tmp_path / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME
    assert not (profile_home / "plugins" / "relic-gumi").exists()
    assert all("gateway" not in command for command in commands)
    assert LifecycleStore(profile_home).current() is LifecycleState.FAILED
    assert not outbound_allowed(profile_home)


def test_installer_creates_private_templates_without_overwriting_user_edits(tmp_path: Path) -> None:
    (tmp_path / "hermes").mkdir()

    def runner(command, **_kwargs):
        if tuple(command[-2:]) == ("gateway", "status"):
            return 0, "healthy", ""
        if tuple(command[-2:]) == ("cron", "status"):
            return 0, "running", ""
        return 0, "", ""

    installer = Installer(
        package_available=lambda _module: True,
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: _NoopManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    profile_home = installer.install().profile_home
    user_template = profile_home / "USER.md"
    environment = profile_home / ".env"
    user_template.write_text("My Gumi preferences.\n", encoding="utf-8")
    environment.write_text("TEST_VALUE=kept\n", encoding="utf-8")
    environment.chmod(0o644)

    installer.install()

    assert (profile_home / "config.yaml").is_file()
    assert (profile_home / "plugins" / "relic-gumi").is_dir()
    assert user_template.read_text(encoding="utf-8") == "My Gumi preferences.\n"
    assert environment.read_text(encoding="utf-8") == "TEST_VALUE=kept\n"
    for path in (environment, profile_home / "SOUL.md", user_template, profile_home / "MEMORY.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    state = json.loads((profile_home / ".relic-gumi-installer.json").read_text(encoding="utf-8"))
    assert set(state["template_fingerprints"]) == {"SOUL.md", "USER.md", "MEMORY.md"}


def test_successful_install_persists_owned_fingerprints(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    (tmp_path / "hermes").mkdir()

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        if tuple(command[-2:]) == ("gateway", "status"):
            return 0, "healthy", ""
        if tuple(command[-2:]) == ("cron", "status"):
            return 0, "running", ""
        return 0, "", ""

    installer = Installer(
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: _NoopManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    result = installer.install()
    state_path = result.profile_home / ".relic-gumi-installer.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert state["ownership"]["plugin_directory"] == "plugins/relic-gumi"
    assert state["plugin_fingerprint"]
    assert state["config_fingerprint"]
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert LifecycleStore(result.profile_home).current() is LifecycleState.GATEWAY_READY
    assert outbound_allowed(result.profile_home)
    assert "cron_checked" in result.plan
    assert "cron_reconciled" in result.plan



def test_installer_reconciles_managed_cron_before_gateway_start(tmp_path: Path) -> None:
    events: list[str] = []
    (tmp_path / "hermes").mkdir()

    class StubManagedCron:
        def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
            events.append(f"reconcile:{delivery_armed}")
            return CronReconciliation(actions=(), created_ids=("new-1",))

        def rollback(self, reconciliation: CronReconciliation) -> None:
            events.append(f"rollback:{','.join(reconciliation.created_ids)}")

    def runner(command, **_kwargs):
        if tuple(command[-2:]) == ("cron", "status"):
            events.append("cron-status")
            return 0, "running", ""
        if tuple(command[-2:]) == ("gateway", "status"):
            events.append("gateway-status")
            return 0, "healthy", ""
        if tuple(command[-2:]) == ("gateway", "restart"):
            events.append("gateway-restart")
            return 0, "", ""
        raise AssertionError(command)

    installer = Installer(
        package_available=lambda _module: True,
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        delivery_armed=lambda _base_home: True,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: StubManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    result = installer.install()

    assert events == [
        "cron-status",
        "reconcile:True",
        "gateway-status",
        "gateway-restart",
        "gateway-status",
    ]
    assert result.plan[-3:] == (
        "cron_reconciled",
        "gateway_restart",
        "delivery_eligible",
    )


def test_installer_can_reconcile_offline_without_starting_gateway(tmp_path: Path) -> None:
    events: list[str] = []
    (tmp_path / "hermes").mkdir()

    class StubManagedCron:
        def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
            events.append(f"reconcile:{delivery_armed}")
            return CronReconciliation(actions=(), created_ids=())

        def rollback(self, reconciliation: CronReconciliation) -> None:
            events.append("rollback")

    def runner(command, **_kwargs):
        if tuple(command[-2:]) == ("cron", "status"):
            events.append("cron-status")
            return 0, "running", ""
        if tuple(command[-2:]) == ("gateway", "status"):
            events.append("gateway-status")
            return 0, "✗ User gateway service is stopped\nActive: inactive (dead)", ""
        raise AssertionError(command)

    installer = Installer(
        package_available=lambda _module: True,
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        delivery_armed=lambda _base_home: True,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: StubManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    result = installer.install(start_gateway=False)

    assert events == ["cron-status", "gateway-status", "reconcile:True"]
    assert result.gateway_actions == ()
    assert "gateway_deferred" in result.plan
    assert LifecycleStore(result.profile_home).current() is LifecycleState.THREE_HOOKS_VERIFIED
    assert not outbound_allowed(result.profile_home)


def test_installer_passes_profile_scripts_directory_to_managed_cron(tmp_path: Path) -> None:
    (tmp_path / "hermes").mkdir()
    factory_calls: list[tuple[object, ...]] = []

    def runner(command, **_kwargs):
        if tuple(command[-2:]) in {
            ("cron", "status"),
            ("gateway", "status"),
            ("gateway", "restart"),
        }:
            return 0, "healthy", ""
        raise AssertionError(command)

    def managed_cron_factory(*args):
        factory_calls.append(args)
        return _NoopManagedCron()

    installer = Installer(
        package_available=lambda _module: True,
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=managed_cron_factory,
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    profile_home = installer.install().profile_home

    assert len(factory_calls) == 1
    assert factory_calls[0][0] == tmp_path / ".relic"
    assert factory_calls[0][2] == profile_home / "scripts"


def test_installer_rolls_back_only_current_managed_cron_creations_on_later_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "hermes").mkdir()
    rollbacks: list[tuple[str, ...]] = []

    class StubManagedCron:
        def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
            assert delivery_armed is False
            return CronReconciliation(actions=(), created_ids=("new-1", "new-2"))

        def rollback(self, reconciliation: CronReconciliation) -> None:
            rollbacks.append(reconciliation.created_ids)

    def runner(command, **_kwargs):
        if tuple(command[-2:]) in {
            ("cron", "status"),
            ("gateway", "status"),
            ("gateway", "restart"),
        }:
            return 0, "healthy", ""
        raise AssertionError(command)

    import relic.hermes_plugin.install as install_module

    monkeypatch.setattr(
        install_module,
        "_write_state",
        lambda _profile_home, _gateway_change, _template_fingerprints: (_ for _ in ()).throw(OSError("state write failed")),
    )
    installer = Installer(
        package_available=lambda _module: True,
        runner=runner,
        verifier=lambda _profile_home: None,
        initialize_user=lambda _profiles_home: None,
        managed_cron_factory=lambda _base_home, _runner, _scripts_dir: StubManagedCron(),
        environ={"HERMES_HOME": str(tmp_path / "hermes")},
        home=tmp_path,
    )

    with pytest.raises(OSError, match="state write failed"):
        installer.install()

    assert rollbacks == [("new-1", "new-2")]



def test_hermes_installed_by_pip_satisfies_prerequisites(tmp_path: Path) -> None:
    available = {"jsonschema", "pydantic", "yaml", "structlog"}
    commands: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        commands.append(tuple(command))
        available.add("hermes_cli")
        return 0, "", ""

    installer = Installer(
        package_available=lambda name: name in available,
        prompt=lambda _question: True,
        runner=runner,
        environ={},
        home=tmp_path,
    )

    installer.ensure_prerequisites()

    assert len(commands) == 1
    assert "hermes-agent>=0.19.1" in commands[0]
