from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from relic.hermes_plugin.lifecycle import LifecycleState, LifecycleStore



REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_KEY = "relic-gumi"
OWNER_MODULE = "hermes_plugins.relic_gumi"
EXPECTED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "transform_llm_output",
    "pre_gateway_dispatch",
)


def _verifier_module():
    module_name = "verify_relic_gumi_test"
    spec = importlib.util.spec_from_file_location(
        module_name, REPOSITORY_ROOT / "scripts" / "verify_plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)


class _Manager:
    def __init__(self, plugin_dir: Path, hooks: dict[str, list], *, enabled: bool = True, error: str | None = None):
        self.discovered = False
        self._plugins = {
            PLUGIN_KEY: SimpleNamespace(
                manifest=SimpleNamespace(source="user", path=plugin_dir),
                module=SimpleNamespace(__name__=OWNER_MODULE),
                enabled=enabled,
                error=error,
                hooks_registered=EXPECTED_HOOKS,
            )
        }
        self._hooks = hooks

    def discover_and_load(self) -> None:
        self.discovered = True


def _configure_host(monkeypatch, manager: _Manager) -> None:
    plugins = importlib.import_module("hermes_cli.plugins")
    monkeypatch.setattr(plugins, "PluginManager", lambda: manager)
def _ready_profile(hermes_home: Path) -> Path:
    profile_home = hermes_home / "profiles" / HERMES_USER_PROFILE_NAME
    plugin_dir = profile_home / "plugins" / PLUGIN_KEY
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.yaml").write_text(
        "name: relic-gumi\nprovides_hooks:\n"
        "  - pre_llm_call\n  - post_llm_call\n  - transform_llm_output\n  - pre_gateway_dispatch\n",
        encoding="utf-8",
    )
    lifecycle = LifecycleStore(profile_home)
    lifecycle.begin()
    for state in (
        LifecycleState.INSTALLED,
        LifecycleState.ENABLED,
        LifecycleState.HOST_V2_LOADED,
        LifecycleState.THREE_HOOKS_VERIFIED,
        LifecycleState.GATEWAY_READY,
    ):
        lifecycle.advance(state)
    return plugin_dir




def test_verifier_selects_owned_callbacks_with_coexisting_plugin(monkeypatch, tmp_path, capsys):
    plugin_dir = _ready_profile(tmp_path)
    calls: list[tuple[str, dict]] = []

    def foreign_pre(**_kwargs):
        raise AssertionError("verifier invoked another plugin's pre hook")

    def owned_pre(**kwargs):
        calls.append(("pre", kwargs))
        return {"context": "verified"}

    def owned_post(**_kwargs):
        raise AssertionError("verifier must not synchronize a turn")

    def foreign_transform(**_kwargs):
        raise AssertionError("verifier invoked another plugin's transform hook")

    def owned_transform(**kwargs):
        calls.append(("transform", kwargs))
        return None

    foreign_pre.__module__ = "hermes_plugins.coexisting"
    owned_pre.__module__ = OWNER_MODULE
    owned_post.__module__ = OWNER_MODULE
    foreign_transform.__module__ = "hermes_plugins.coexisting"
    owned_transform.__module__ = OWNER_MODULE

    def owned_gateway(**_kwargs):
        raise AssertionError("verifier must not dispatch a gateway message")

    owned_gateway.__module__ = OWNER_MODULE
    manager = _Manager(
        plugin_dir,
        {
            "pre_llm_call": [foreign_pre, owned_pre],
            "post_llm_call": [owned_post],
            "transform_llm_output": [foreign_transform, owned_transform],
            "pre_gateway_dispatch": [owned_gateway],
        },
    )
    _configure_host(monkeypatch, manager)

    verifier = _verifier_module()
    assert verifier.main(["--hermes-home", str(tmp_path)]) == 0
    assert manager.discovered
    assert [name for name, _ in calls] == ["pre", "transform"]
    assert all(call["execution_context"] == "foreground" for _, call in calls)
    assert all(call["user_visible"] is True for _, call in calls)
    assert capsys.readouterr().out == "relic-gumi verified\n"


@pytest.mark.parametrize(
    ("enabled", "error", "message"),
    [
        (False, "disabled via config", "disabled"),
        (True, "registration failed", "plugin error"),
    ],
)
def test_verifier_rejects_unusable_plugin(
    monkeypatch, tmp_path, capsys, enabled, error, message
):
    plugin_dir = _ready_profile(tmp_path)

    def callback(**_kwargs):
        return None

    callback.__module__ = OWNER_MODULE
    manager = _Manager(
        plugin_dir,
        {hook_name: [callback] for hook_name in EXPECTED_HOOKS},
        enabled=enabled,
        error=error,
    )
    _configure_host(monkeypatch, manager)

    verifier = _verifier_module()
    assert verifier.main(["--hermes-home", str(tmp_path)]) == 1
    assert message in capsys.readouterr().err


def test_verifier_requires_gateway_ready_lifecycle(monkeypatch, tmp_path, capsys):
    plugin_dir = tmp_path / "profiles" / HERMES_USER_PROFILE_NAME / "plugins" / PLUGIN_KEY
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: relic-gumi\n", encoding="utf-8")

    def callback(**_kwargs):
        return None

    callback.__module__ = OWNER_MODULE
    _configure_host(
        monkeypatch,
        _Manager(plugin_dir, {hook_name: [callback] for hook_name in EXPECTED_HOOKS}),
    )

    verifier = _verifier_module()
    assert verifier.main(["--hermes-home", str(tmp_path)]) == 1
    assert "lifecycle" in capsys.readouterr().err


def test_verifier_and_readme_use_canonical_profile_name_constant(monkeypatch):
    import relic.hermes_runtime as hermes_runtime

    monkeypatch.setattr(hermes_runtime, "HERMES_USER_PROFILE_NAME", "custom-relic-profile")
    verifier = _verifier_module()
    assert verifier.PROFILE_NAME == "custom-relic-profile"

    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    assert "the installer writes the `gumi` Hermes profile" not in readme
    assert f"the installer writes the `{HERMES_USER_PROFILE_NAME}` Hermes profile" in readme

