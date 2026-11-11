"""Packaging contracts; never invoke the real Hermes PM or its launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml

from relic.hermes_plugin import install as installer_module
from relic.hermes_plugin.install import (
    Installer, InstallerError, PrerequisiteError, install_directory_plugin,
    plugin_artifact_source,
)

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_manifests_match():
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    for manifest in (ROOT / "plugin.yaml", plugin_artifact_source() / "plugin.yaml"):
        assert yaml.safe_load(manifest.read_text())["python_dependencies"] == dependencies
    assert all(">=" in item and ",<" in item for item in dependencies)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime" / "relic"
    module = root / "hermes_plugin" / "install.py"
    module.parent.mkdir(parents=True)
    module.write_text("version = 1\n")
    (root / "__init__.py").write_text("")
    monkeypatch.setattr(installer_module, "__file__", str(module))
    source = tmp_path / "entry"
    source.mkdir()
    (source / "__init__.py").write_text("def register(ctx): pass\n")
    (source / "plugin.yaml").write_text("name: relic-gumi\n")
    return root, source


def test_vendor_exclusions_and_hash_track_only_shipped_runtime(runtime):
    root, source = runtime
    profile = root / "output-profile"
    for relative in ("tests/fake.py", "__pycache__/noise", "hermes_plugin/gumi_entry/plugin.yaml", ".pytest_cache/noise", "nested/noise.pyo"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not runtime")
    first = install_directory_plugin(profile, source=source)
    first.commit()
    vendor = first.path / "_vendor" / "relic"
    assert (vendor / "hermes_plugin" / "install.py").is_file()
    for excluded in ("tests", "__pycache__", "hermes_plugin/gumi_entry", ".pytest_cache", "nested/noise.pyo", "output-profile"):
        assert not (vendor / excluded).exists(), excluded
    assert (first.path / ".relic-runtime.sha256").read_text().strip() == installer_module._directory_fingerprint(vendor)
    (root / "tests" / "fake.py").write_text("changed but excluded")
    assert not install_directory_plugin(profile, source=source).changed
    (root / "hermes_plugin" / "install.py").write_text("version = 2\n")
    updated = install_directory_plugin(profile, source=source)
    assert updated.changed
    updated.rollback()
    assert (vendor / "hermes_plugin" / "install.py").read_text() == "version = 1\n"
    assert not list(first.path.parent.glob(".relic-gumi.*"))


def test_vendor_rejects_symlinks_without_traversing_them(runtime, tmp_path):
    root, source = runtime
    (root / "loop").symlink_to(root, target_is_directory=True)
    profile = tmp_path / "profile"
    with pytest.raises(InstallerError, match="Invalid runtime artifact entry"):
        install_directory_plugin(profile, source=source)
    assert not list((profile / "plugins").iterdir())


@pytest.mark.parametrize("layout, expected", [
    ("installs/release/environments/generation/venv", True),
    ("environments/generation/venv", False),
    ("venv", False),
])
def test_pm_detection(layout, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / layout))
    assert installer_module._running_in_pm_environment() is expected


def test_pm_missing_requirements_never_prompt_or_pip(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("PM-managed environments must not be mutated with pip")
    installer = Installer(pm_managed=lambda: True, package_available=lambda _: False,
                          runner=forbidden, prompt=forbidden, environ={}, home=tmp_path)
    with pytest.raises(PrerequisiteError, match="does not pip-install"):
        installer.ensure_prerequisites()


def test_dependency_modes(tmp_path):
    for managed, expected in ((True, "pm"), (False, "present")):
        assert Installer(pm_managed=lambda: managed, package_available=lambda _: True,
                         environ={}, home=tmp_path).ensure_prerequisites() == expected


@pytest.mark.parametrize("failure", [False, True])
def test_pm_sync_is_injected_scoped_and_rolls_back(tmp_path, failure):
    from relic.hermes_plugin.managed_cron import CronReconciliation
    home = tmp_path / "hermes"
    home.mkdir()
    profile = home / "profiles" / "relic-gumi"
    profile.mkdir(parents=True)
    (profile / ".relic-gumi-installer.json").write_text("{}")
    (profile / "config.yaml").write_text("model:\n  default: keep\n")
    old = profile / "plugins" / "relic-gumi"
    old.mkdir(parents=True)
    (old / "old").write_text("keep")
    calls = []
    verified = []
    class Cron:
        def reconcile(self, **kwargs):
            return CronReconciliation(actions=(), created_ids=())
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if tuple(command[-3:]) == ("plugins", "enable", "relic-gumi"):
            assert (old / "_vendor" / "relic" / "__init__.py").is_file()
            assert yaml.safe_load((profile / "config.yaml").read_text())["plugins"]["enabled"] == ["relic-gumi"]
            assert not verified
            return (1, "", "injected resolution failure") if failure else (0, "prepared", "")
        assert tuple(command[-2:]) in {("gateway", "status"), ("cron", "status")}
        return 0, "stopped" if "gateway" in command else "healthy", ""
    installer = Installer(
        pm_managed=lambda: True, package_available=lambda _: True, runner=runner,
        which=lambda _: "fake-hermes", verifier=lambda path: verified.append(path),
        initialize_user=lambda _: None, managed_cron_factory=lambda *args: Cron(),
        environ={"HOME": str(tmp_path), "HERMES_HOME": str(home), "RELIC_HOME": str(tmp_path / "relic")},
        home=tmp_path,
    )
    if failure:
        with pytest.raises(InstallerError, match="injected resolution failure"):
            installer.install(start_gateway=False)
        assert (old / "old").read_text() == "keep"
        assert (profile / "config.yaml").read_text() == "model:\n  default: keep\n"
        assert not verified
    else:
        result = installer.install(start_gateway=False)
        assert result.dependency_mode == "pm"
        assert verified == [profile]
    sync = [(cmd, options) for cmd, options in calls if "enable" in cmd]
    assert len(sync) == 1
    command, options = sync[0]
    assert command == ["fake-hermes", "--profile", "relic-gumi", "plugins", "enable", "relic-gumi"]
    assert options["env"]["HERMES_HOME"] == str(home)
    assert options["timeout"] == 600


def test_generated_action_scripts_resolve_profile_vendor(tmp_path):
    from relic.hermes_plugin.managed_cron import _canonical_action_script
    profile = tmp_path / "profile"
    plugin = install_directory_plugin(profile).path
    script = profile / "scripts" / "checkin.sh"
    script.parent.mkdir()
    script.write_text(_canonical_action_script("checkin-message")(script))
    relic_home = tmp_path / "relic"
    user = relic_home / "user" / "user_profile.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"runtime_scope_id": "a" * 64}))
    user.chmod(0o600)
    # Observe which runtime the shell action and its future worker will import,
    # without running delivery or using any Hermes launcher.
    shim = tmp_path / "python-shim"
    import shlex
    shim.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "$1" == "-m" ]]; then\n'
        f'  exec {shlex.quote(sys.executable)} -c "import relic; print(relic.__file__)"\n'
        'fi\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    shim.chmod(0o700)
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(profile), RELIC_HOME=str(relic_home), RELIC_PYTHON=str(shim))
    env.pop("PYTHONPATH", None)
    result = subprocess.run(["bash", str(script), "--help"], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == plugin / "_vendor" / "relic" / "__init__.py"


@pytest.mark.parametrize("kind", ["root", "copied"])
def test_plugin_imports_without_installed_relic(tmp_path, kind):
    entry = ROOT if kind == "root" else install_directory_plugin(tmp_path / "profile").path
    script = '''
import importlib.util, json, pathlib, sys
entry = pathlib.Path(sys.argv[1])
sys.path[:] = [p for p in sys.path if not (pathlib.Path(p) / 'relic').exists()]
assert importlib.util.find_spec('relic') is None
spec = importlib.util.spec_from_file_location('packaging_plugin', entry / '__init__.py', submodule_search_locations=[str(entry)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
import relic
from importlib.resources import files
assert files(relic).joinpath('db/migrations/0001_initial.sql').is_file()
from relic.context_pack.schema import SCHEMA_PATH
assert SCHEMA_PATH.is_file()
assert callable(module.register)
import os
os.environ.pop('PYTHONPATH', None)
from relic.hermes_plugin import directory_plugin
captured = {}
def capture(command, **options):
    captured.update(options)
    from types import SimpleNamespace
    import io
    return SimpleNamespace(stdin=io.StringIO())
original = __import__('subprocess').Popen
__import__('subprocess').Popen = capture
try:
    directory_plugin._spawn_dispatch('probe', 'checkin', pathlib.Path(os.environ['HERMES_HOME']), os.environ['RELIC_HOME'], 'a' * 64)
finally:
    __import__('subprocess').Popen = original
child = __import__('subprocess').run([sys.executable, '-c', 'import relic; print(relic.__file__)'], env=captured['env'], capture_output=True, text=True, timeout=30)
assert child.returncode == 0, child.stderr
assert pathlib.Path(child.stdout.strip()) == pathlib.Path(relic.__file__)
print(json.dumps({'runtime': relic.__file__}))
'''
    env = dict(os.environ, HOME=str(tmp_path / "home"), HERMES_HOME=str(tmp_path / "hermes"), RELIC_HOME=str(tmp_path / "relic"))
    result = subprocess.run([sys.executable, "-I", "-c", script, str(entry)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    runtime = Path(json.loads(result.stdout.splitlines()[-1])["runtime"])
    assert runtime == entry / ("src/relic/__init__.py" if kind == "root" else "_vendor/relic/__init__.py")


@pytest.mark.parametrize("kind", ["root", "profile"])
def test_bundled_runtime_wins_over_other_relic_on_path(tmp_path, kind):
    entry = ROOT if kind == "root" else install_directory_plugin(tmp_path / "profile").path
    other = tmp_path / "other"
    (other / "relic").mkdir(parents=True)
    (other / "relic" / "__init__.py").write_text("MARK = 'external'\n")
    script = '''
import importlib.util, json, pathlib, sys
entry = pathlib.Path(sys.argv[1])
sys.path[:] = [p for p in sys.path if not (pathlib.Path(p) / 'relic').exists()]
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location('packaging_plugin', entry / '__init__.py', submodule_search_locations=[str(entry)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
import relic
print(json.dumps({'runtime': relic.__file__}))
'''
    env = dict(os.environ, HOME=str(tmp_path / "home"), HERMES_HOME=str(tmp_path / "hermes"), RELIC_HOME=str(tmp_path / "relic"))
    result = subprocess.run([sys.executable, "-I", "-c", script, str(entry), str(other)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    runtime = Path(json.loads(result.stdout.splitlines()[-1])["runtime"])
    assert runtime == entry / ("src/relic/__init__.py" if kind == "root" else "_vendor/relic/__init__.py")
