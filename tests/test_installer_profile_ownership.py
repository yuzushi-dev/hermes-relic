"""The installer owns ``profiles/relic-gumi`` and refuses a profile it did not create."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from relic.hermes_plugin.install import (
    STATE_FILENAME,
    ForeignProfileError,
    Installer,
    InstallerError,
)
from relic.hermes_plugin.lifecycle import LifecycleState, LifecycleStore
from relic.hermes_plugin.managed_cron import CronReconciliation
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME
from relic.profile.registry import ProfileRegistry, UserProfile
from relic import user_runtime

OLD_PROFILE_NAME = "gumi"


@pytest.fixture(autouse=True)
def _isolated_process_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default code paths read RELIC_HOME/HOME from the process: never let them reach the real ones."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RELIC_HOME", str(tmp_path / "relic"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("HERMES_PROFILES_HOME", raising=False)


class _NoopManagedCron:
    def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
        return CronReconciliation(actions=(), created_ids=())

    def rollback(self, reconciliation: CronReconciliation) -> None:
        pass

    def configure_hermes_jobs_path(self, _profile_home) -> None:
        pass

    def remove_all(self):
        return ()


def _runner(command, **_kwargs):
    if tuple(command[-2:]) == ("gateway", "status"):
        return 0, "stopped", ""  # --no-start requires the gateway to be down
    if tuple(command[-2:]) == ("cron", "status"):
        return 0, "healthy", ""
    return 0, "", ""


def _installer(tmp_path: Path, **overrides) -> Installer:
    settings = dict(
        initialize_user=lambda profiles_home: ProfileRegistry(
            relic_home=tmp_path / "relic", hermes_profiles_home=profiles_home
        ).initialize_user(),
        package_available=lambda _module: True,
        which=lambda _name: "hermes",
        runner=_runner,
        verifier=lambda _profile_home: None,
        managed_cron_factory=lambda _base, _runner, _scripts: _NoopManagedCron(),
        environ={
            "HERMES_HOME": str(tmp_path / "hermes"),
            "RELIC_HOME": str(tmp_path / "relic"),
        },
        home=tmp_path,
    )
    settings.update(overrides)
    return Installer(**settings)


def _base(tmp_path: Path) -> Path:
    base = tmp_path / "hermes"
    base.mkdir(exist_ok=True)
    return base


def _snapshot(root: Path) -> dict[str, tuple[str, str, int]]:
    """Relative path -> (kind, sha256 or link target, mode) for every entry under root."""
    result: dict[str, tuple[str, str, int]] = {}
    for path in sorted([root, *root.rglob("*")]):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path), mode)
        elif path.is_dir():
            result[relative] = ("dir", "", mode)
        else:
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), mode)
    return result


def test_profile_name_is_the_single_constant_and_not_the_old_name() -> None:
    from relic.hermes_plugin import install

    assert HERMES_USER_PROFILE_NAME == "relic-gumi"
    assert install.PROFILE_NAME == HERMES_USER_PROFILE_NAME
    assert user_runtime.PROFILE_NAME == HERMES_USER_PROFILE_NAME


def test_clean_install_creates_only_the_new_profile_and_no_old_one(tmp_path: Path) -> None:
    base = _base(tmp_path)

    result = _installer(tmp_path).install(start_gateway=False)

    assert result.profile_home == base.resolve() / "profiles" / HERMES_USER_PROFILE_NAME
    assert [entry.name for entry in (base / "profiles").iterdir()] == [HERMES_USER_PROFILE_NAME]
    assert not (base / "profiles" / OLD_PROFILE_NAME).exists()
    state = json.loads((result.profile_home / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["profile"] == HERMES_USER_PROFILE_NAME
    user = json.loads((tmp_path / "relic" / "user" / "user_profile.json").read_text("utf-8"))
    assert user["hermes_profile_name"] == HERMES_USER_PROFILE_NAME


def test_second_install_is_idempotent(tmp_path: Path) -> None:
    _base(tmp_path)
    installer = _installer(tmp_path)

    first = installer.install(start_gateway=False)
    after_first = {k: v for k, v in _snapshot(first.profile_home).items() if not k.startswith(".relic-gumi")}
    second = installer.install(start_gateway=False)

    assert second.plugin_changed is False
    assert second.config_changed is False
    after_second = {k: v for k, v in _snapshot(second.profile_home).items() if not k.startswith(".relic-gumi")}
    assert after_second == after_first


def test_foreign_profile_is_refused_with_no_writes(tmp_path: Path) -> None:
    base = _base(tmp_path)
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME
    (profile / "memories").mkdir(parents=True)
    (profile / "config.yaml").write_text("model: someone-else\n", encoding="utf-8")
    (profile / ".env").write_text("SECRET_TOKEN=do-not-print\n", encoding="utf-8")
    (profile / ".env").chmod(0o600)
    (profile / "memories" / "notes.md").write_text("mine\n", encoding="utf-8")
    before = _snapshot(base)

    with pytest.raises(ForeignProfileError) as raised:
        _installer(tmp_path).install(start_gateway=False)

    assert _snapshot(base) == before
    assert not (profile / ".relic-gumi").exists()
    assert not (tmp_path / "relic").exists()
    message = str(raised.value)
    assert str(profile) in message
    assert "not created by the relic-gumi installer" in message
    assert "not modified" in message
    assert "--hermes-home" in message and "rename" in message
    assert "do-not-print" not in message
    assert isinstance(raised.value, InstallerError)


def test_foreign_profile_is_refused_even_if_it_only_has_an_env_file(tmp_path: Path) -> None:
    base = _base(tmp_path)
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME
    profile.mkdir(parents=True)
    (profile / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    before = _snapshot(base)

    with pytest.raises(ForeignProfileError):
        _installer(tmp_path).install(start_gateway=False)

    assert _snapshot(base) == before


def test_symlinked_profile_is_refused(tmp_path: Path) -> None:
    base = _base(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (base / "profiles").mkdir()
    (base / "profiles" / HERMES_USER_PROFILE_NAME).symlink_to(elsewhere, target_is_directory=True)
    before = _snapshot(elsewhere)

    with pytest.raises(ForeignProfileError, match="symbolic link"):
        _installer(tmp_path).install(start_gateway=False)

    assert _snapshot(elsewhere) == before


def test_empty_profile_directory_is_accepted(tmp_path: Path) -> None:
    base = _base(tmp_path)
    (base / "profiles" / HERMES_USER_PROFILE_NAME).mkdir(parents=True)

    result = _installer(tmp_path).install(start_gateway=False)

    assert (result.profile_home / STATE_FILENAME).is_file()


@pytest.mark.parametrize("marker", [STATE_FILENAME, ".relic-gumi/lifecycle.json"])
def test_profile_with_an_installer_marker_is_accepted(tmp_path: Path, marker: str) -> None:
    base = _base(tmp_path)
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME
    (profile / marker).parent.mkdir(parents=True, exist_ok=True)
    (profile / marker).write_text('{"state": "preflight"}\n', encoding="utf-8")
    (profile / "other-file").write_text("kept\n", encoding="utf-8")

    result = _installer(tmp_path).install(start_gateway=False)

    assert result.profile_home == profile.resolve()
    assert (profile / "other-file").read_text(encoding="utf-8") == "kept\n"


def test_failed_install_leaves_a_profile_the_next_run_accepts(tmp_path: Path) -> None:
    base = _base(tmp_path)
    attempts: list[int] = []

    def flaky_initialize_user(profiles_home: Path):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("user initialization failed")
        return ProfileRegistry(
            relic_home=tmp_path / "relic", hermes_profiles_home=profiles_home
        ).initialize_user()

    installer = _installer(tmp_path, initialize_user=flaky_initialize_user)
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME

    with pytest.raises(RuntimeError, match="user initialization failed"):
        installer.install(start_gateway=False)

    # What a failure leaves behind: only the lifecycle marker, in the failed state.
    assert sorted(_snapshot(profile)) == [".", ".relic-gumi", ".relic-gumi/lifecycle.json"]
    assert LifecycleStore(profile).current() is LifecycleState.FAILED

    result = installer.install(start_gateway=False)

    assert len(attempts) == 2
    assert (result.profile_home / STATE_FILENAME).is_file()
    assert not (base / "profiles" / OLD_PROFILE_NAME).exists()


def test_old_gumi_profile_is_ignored_and_left_identical(tmp_path: Path) -> None:
    base = _base(tmp_path)
    old = base / "profiles" / OLD_PROFILE_NAME
    (old / "memories").mkdir(parents=True)
    (old / "config.yaml").write_text("model: mine\n", encoding="utf-8")
    (old / ".env").write_text("TELEGRAM_BOT_TOKEN=keep-me\n", encoding="utf-8")
    (old / ".env").chmod(0o600)
    (old / "memories" / "m.md").write_text("old memory\n", encoding="utf-8")
    old.chmod(0o750)
    before = _snapshot(old)

    result = _installer(tmp_path).install(start_gateway=False)

    assert _snapshot(old) == before
    assert result.profile_home == base.resolve() / "profiles" / HERMES_USER_PROFILE_NAME
    assert sorted(entry.name for entry in (base / "profiles").iterdir()) == sorted(
        [OLD_PROFILE_NAME, HERMES_USER_PROFILE_NAME]
    )


def test_old_gumi_profile_is_ignored_by_uninstall(tmp_path: Path) -> None:
    base = _base(tmp_path)
    old = base / "profiles" / OLD_PROFILE_NAME
    (old / "plugins" / "relic-gumi").mkdir(parents=True)
    (old / "plugins" / "relic-gumi" / "__init__.py").write_text("", encoding="utf-8")
    (old / "config.yaml").write_text("plugins:\n  enabled:\n    - relic-gumi\n", encoding="utf-8")
    before = _snapshot(old)

    result = _installer(tmp_path).uninstall()

    assert _snapshot(old) == before
    assert result.plugin_removed is False
    assert result.removed_jobs == ()


def test_uninstall_does_not_touch_a_foreign_profile(tmp_path: Path) -> None:
    base = _base(tmp_path)
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME
    (profile / "plugins" / "relic-gumi").mkdir(parents=True)
    (profile / "plugins" / "relic-gumi" / "__init__.py").write_text("", encoding="utf-8")
    (profile / "config.yaml").write_text("plugins:\n  enabled:\n    - relic-gumi\n", encoding="utf-8")
    before = _snapshot(base)

    with pytest.raises(ForeignProfileError, match="not modified"):
        _installer(tmp_path).uninstall()

    assert _snapshot(base) == before


def test_user_profile_with_another_profile_name_is_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hermes_profile_name must be"):
        UserProfile(
            runtime_scope_id="a" * 64,
            hermes_profile_name=OLD_PROFILE_NAME,
            user_home=tmp_path / "user",
        )
    registry = ProfileRegistry(
        relic_home=tmp_path / "relic", hermes_profiles_home=tmp_path / "profiles"
    )
    registry.initialize_user()
    stored = json.loads(registry.user_profile_path.read_text(encoding="utf-8"))
    stored["hermes_profile_name"] = OLD_PROFILE_NAME
    registry.user_profile_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ValueError, match="hermes_profile_name must be"):
        registry.load_user()


def test_hermes_profile_home_heuristic_resolves_profile_and_base(tmp_path: Path) -> None:
    base = tmp_path / "hermes"
    profile = base / "profiles" / HERMES_USER_PROFILE_NAME
    profile.mkdir(parents=True)

    # HERMES_HOME already pointing at the profile -> the profile itself.
    assert user_runtime._hermes_profile_home({"HERMES_HOME": str(profile)}) == profile
    assert user_runtime._hermes_profile_home({}, hermes_home=profile) == profile
    # HERMES_HOME pointing at the Hermes base -> <base>/profiles/<profile>.
    assert user_runtime._hermes_profile_home({"HERMES_HOME": str(base)}) == profile
    assert user_runtime._hermes_profile_home({}, hermes_home=base) == profile
    # A directory with a .env is treated as a profile whatever its name (inherited rule).
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("", encoding="utf-8")
    assert user_runtime._hermes_profile_home({"HERMES_HOME": str(other)}) == other
    # The old name is no longer recognised as the profile.
    old = base / "profiles" / OLD_PROFILE_NAME
    old.mkdir()
    assert user_runtime._hermes_profile_home({"HERMES_HOME": str(old)}) == old / "profiles" / HERMES_USER_PROFILE_NAME
