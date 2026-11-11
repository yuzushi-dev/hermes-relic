#!/usr/bin/env python3
"""Verify relic-gumi through Hermes' directory plugin loader."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


from relic.hermes_runtime import HERMES_USER_PROFILE_NAME


PLUGIN_KEY = "relic-gumi"
PROFILE_NAME = HERMES_USER_PROFILE_NAME


def _verify_artifact(profile_home: Path) -> Path:
    plugin_dir = profile_home / "plugins" / PLUGIN_KEY
    manifest_path = plugin_dir / "plugin.yaml"
    entry_path = plugin_dir / "__init__.py"
    if (
        plugin_dir.is_symlink()
        or not plugin_dir.is_dir()
        or entry_path.is_symlink()
        or not entry_path.is_file()
        or manifest_path.is_symlink()
    ):
        raise RuntimeError("relic-gumi canonical plugin artifact is missing or linked")
    try:
        import yaml

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("relic-gumi canonical plugin artifact is missing") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != PLUGIN_KEY:
        raise RuntimeError("relic-gumi canonical plugin artifact has an invalid manifest")
    if set(manifest.get("provides_hooks", ())) != set(EXPECTED_HOOKS):
        raise RuntimeError("relic-gumi canonical plugin artifact declares an invalid hook set")
    return plugin_dir


def _verify_lifecycle(profile_home: Path) -> None:
    from relic.hermes_plugin.lifecycle import LifecycleStore

    if not LifecycleStore(profile_home).ready():
        raise RuntimeError("relic-gumi lifecycle is not gateway-ready")


EXPECTED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "transform_llm_output",
    "pre_gateway_dispatch",
)



def _owned_callbacks(manager, owner_module):
    callbacks_by_hook = {}
    for hook_name in EXPECTED_HOOKS:
        owned = [
            callback
            for callback in manager._hooks.get(hook_name, ())
            if getattr(callback, "__module__", None) == owner_module
        ]
        if len(owned) != 1:
            raise RuntimeError(
                f"relic-gumi expected exactly one owned {hook_name} callback; "
                f"found {len(owned)}"
            )
        callbacks_by_hook[hook_name] = owned[0]

    for hook_name, callbacks in manager._hooks.items():
        if hook_name not in EXPECTED_HOOKS and any(
            getattr(callback, "__module__", None) == owner_module
            for callback in callbacks
        ):
            raise RuntimeError(f"relic-gumi registered unexpected {hook_name} callback")
    return callbacks_by_hook


def _verify_functional_callbacks(callbacks):
    foreground = {"execution_context": "foreground", "user_visible": True}
    pre_result = callbacks["pre_llm_call"](
        **foreground,
        session_id="relic-gumi-verification",
        user_message="",
    )
    if pre_result is not None and (
        not isinstance(pre_result, dict) or not isinstance(pre_result.get("context"), str)
    ):
        raise RuntimeError("relic-gumi pre_llm_call returned an invalid result")

    transform_result = callbacks["transform_llm_output"](
        **foreground,
        response_text="relic-gumi verification",
    )
    if transform_result is not None and not isinstance(transform_result, str):
        raise RuntimeError("relic-gumi transform_llm_output returned an invalid result")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify the relic-gumi plugin.")
    parser.add_argument("--hermes-home", required=True, type=Path)
    args = parser.parse_args(argv)

    hermes_home = args.hermes_home.expanduser().resolve()
    profile_home = hermes_home / "profiles" / PROFILE_NAME
    plugin_dir = profile_home / "plugins" / PLUGIN_KEY
    previous_home = os.environ.get("HERMES_HOME")
    previous_project_plugins = os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS")
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

    try:
        _verify_lifecycle(profile_home)
        _verify_artifact(profile_home)
        try:
            from hermes_cli.plugins import PluginManager
        except ImportError:
            print(
                "relic-gumi verification requires Hermes; run this script with the "
                "Python interpreter containing Hermes.",
                file=sys.stderr,
            )
            return 1

        manager = PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins.get(PLUGIN_KEY)
        if loaded is None:
            raise RuntimeError("relic-gumi plugin not discovered")
        if (
            loaded.manifest.source != "user"
            or loaded.manifest.path is None
            or Path(loaded.manifest.path).resolve() != plugin_dir
        ):
            raise RuntimeError("relic-gumi plugin not loaded from supplied Hermes home")
        if not loaded.enabled:
            reason = f": {loaded.error}" if loaded.error else ""
            raise RuntimeError(f"relic-gumi plugin is disabled{reason}")
        if loaded.error is not None:
            raise RuntimeError(f"relic-gumi plugin error: {loaded.error}")
        if set(loaded.hooks_registered) != set(EXPECTED_HOOKS):
            raise RuntimeError("relic-gumi registered an unexpected hook set")

        owner_module = getattr(loaded.module, "__name__", "")
        if not owner_module:
            raise RuntimeError("relic-gumi did not provide a loaded module")
        _verify_functional_callbacks(_owned_callbacks(manager, owner_module))
    except Exception as exc:
        print(f"relic-gumi verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
        if previous_project_plugins is None:
            os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
        else:
            os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = previous_project_plugins

    print("relic-gumi verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
