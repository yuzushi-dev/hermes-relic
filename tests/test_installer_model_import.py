"""The installer imports the base config's default provider and model into the profile."""

from __future__ import annotations

import builtins
import hashlib
from pathlib import Path

import pytest
import yaml

from relic.hermes_plugin import install as install_module
from relic.hermes_plugin.install import (
    HermesLocation,
    Installer,
    InstallerError,
    import_default_model,
)
from relic.hermes_plugin.managed_cron import CronReconciliation
from relic.profile.registry import ProfileRegistry
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

PROFILE = HERMES_USER_PROFILE_NAME
FAKE_MODEL = {"provider": "example-provider", "default": "example-model"}


@pytest.fixture(autouse=True)
def _isolated_process_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        return 0, "stopped", ""
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
        environ={"HERMES_HOME": str(tmp_path / "hermes"), "RELIC_HOME": str(tmp_path / "relic")},
        home=tmp_path,
    )
    settings.update(overrides)
    return Installer(**settings)


def _base(tmp_path: Path, config: object | None = None, *, raw: str | None = None) -> Path:
    base = tmp_path / "hermes"
    base.mkdir(exist_ok=True)
    if raw is not None:
        (base / "config.yaml").write_text(raw, encoding="utf-8")
    elif config is not None:
        (base / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return base


def _profile_config(base: Path) -> dict:
    path = base / "profiles" / PROFILE / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _location(base: Path) -> HermesLocation:
    return HermesLocation(base, base / "profiles" / PROFILE, "default")


def test_new_profile_receives_provider_and_model(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})

    result = _installer(tmp_path).install(start_gateway=False)

    assert _profile_config(base)["model"] == FAKE_MODEL
    assert result.model_import is not None and result.model_import.changed
    assert (result.model_import.provider, result.model_import.model) == ("example-provider", "example-model")
    assert result.config_changed is True
    assert any("example-provider" in m and "example-model" in m for m in result.model_import.messages)


def test_optional_keys_api_mode_and_base_url_are_imported_when_present(tmp_path: Path) -> None:
    model = {**FAKE_MODEL, "api_mode": "chat_completions", "base_url": "https://llm.example.invalid/v1"}
    base = _base(tmp_path, {"model": model})

    _installer(tmp_path).install(start_gateway=False)

    assert _profile_config(base)["model"] == model


@pytest.mark.parametrize(
    "base_url", ["https://user:pw@llm.example.invalid/v1", "https://llm.example.invalid/v1?token=abc"]
)
def test_base_url_with_userinfo_or_query_is_dropped_with_a_notice(tmp_path: Path, base_url: str) -> None:
    base = _base(tmp_path, {"model": {**FAKE_MODEL, "api_mode": "chat_completions", "base_url": base_url}})

    result = _installer(tmp_path).install(start_gateway=False)

    model = _profile_config(base)["model"]
    assert model == {**FAKE_MODEL, "api_mode": "chat_completions"}
    assert "pw" not in str(result.model_import.messages) and "abc" not in str(result.model_import.messages)
    assert any("base_url" in m for m in result.model_import.messages)


def test_credential_keys_in_the_base_model_block_are_not_copied(tmp_path: Path) -> None:
    base = _base(
        tmp_path,
        {"model": {**FAKE_MODEL, "api_key": "sk-fake-secret", "token": "tok-fake", "extra": "x"}},
    )

    result = _installer(tmp_path).install(start_gateway=False)

    text = (base / "profiles" / PROFILE / "config.yaml").read_text(encoding="utf-8")
    assert "sk-fake-secret" not in text and "tok-fake" not in text
    assert _profile_config(base)["model"] == FAKE_MODEL
    assert "sk-fake-secret" not in str(result.model_import.messages)


def test_existing_profile_model_is_left_byte_identical(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    profile = base / "profiles" / PROFILE
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    # A profile holding only a default is the user's own choice: even the missing provider stays missing.
    config.write_text("model:\n  default: own-model\n", encoding="utf-8")
    (profile / ".relic-gumi-installer.json").write_text("{}", encoding="utf-8")
    before = _sha(config)

    result = import_default_model(_location(base), profile)

    assert result.changed is False
    assert _sha(config) == before


def test_install_keeps_a_profile_model_the_user_set_by_hand(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    profile = base / "profiles" / PROFILE
    (profile / ".relic-gumi").mkdir(parents=True)
    (profile / ".relic-gumi" / "lifecycle.json").write_text("{}", encoding="utf-8")
    (profile / "config.yaml").write_text(
        "model:\n  provider: own-provider\n  default: own-model\n", encoding="utf-8"
    )

    _installer(tmp_path).install(start_gateway=False)

    assert _profile_config(base)["model"] == {"provider": "own-provider", "default": "own-model"}


def test_second_install_is_idempotent_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    installer = _installer(tmp_path)
    first = installer.install(start_gateway=False)
    config = first.profile_home / "config.yaml"
    before = _sha(config)

    written: list[Path] = []
    original = install_module._write_config
    monkeypatch.setattr(
        install_module, "_write_config", lambda path, value: (written.append(path), original(path, value))[1]
    )
    second = installer.install(start_gateway=False)

    assert second.model_import is not None and second.model_import.changed is False
    assert second.config_changed is False
    assert _sha(config) == before
    assert config not in written
    assert _profile_config(base)["model"] == FAKE_MODEL


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"model": None},
        {"model": ""},
        {"model": "just-a-string"},
        {"model": ["a"]},
        {"model": {}},
        {"model": {"provider": "example-provider"}},
        {"model": {"default": "example-model"}},
        {"model": {"provider": "  ", "default": "example-model"}},
    ],
    ids=["no-file", "empty", "none", "blank", "string", "list", "empty-map", "no-default", "no-provider", "blank-provider"],
)
def test_unusable_base_model_imports_nothing_and_says_how_to_set_it(tmp_path: Path, config) -> None:
    base = _base(tmp_path, config)

    result = _installer(tmp_path).install(start_gateway=False)

    assert "model" not in _profile_config(base)
    assert result.model_import.changed is False
    (message,) = result.model_import.messages
    assert "No default model to import" in message
    assert f"hermes --profile {PROFILE} config set model.provider" in message
    assert f"hermes --profile {PROFILE} config set model.default" in message


@pytest.mark.parametrize("raw", ["- not\n- a mapping\n", "model: [unclosed\n", "just text\n"])
def test_corrupt_base_config_is_a_clear_error_with_no_partial_write(tmp_path: Path, raw: str) -> None:
    base = _base(tmp_path, raw=raw)

    with pytest.raises(InstallerError):
        _installer(tmp_path).install(start_gateway=False)

    assert "model" not in _profile_config(base)


def test_corrupt_profile_config_is_a_clear_error(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    profile = base / "profiles" / PROFILE
    profile.mkdir(parents=True)
    (profile / ".relic-gumi-installer.json").write_text("{}", encoding="utf-8")
    (profile / "config.yaml").write_text("- list\n", encoding="utf-8")
    before = _sha(profile / "config.yaml")

    with pytest.raises(InstallerError):
        _installer(tmp_path).install(start_gateway=False)

    assert _sha(profile / "config.yaml") == before


def test_profile_model_of_wrong_type_is_an_error_not_an_overwrite(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    profile = base / "profiles" / PROFILE
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: [1, 2]\n", encoding="utf-8")
    before = _sha(profile / "config.yaml")

    with pytest.raises(InstallerError, match="Expected model mapping"):
        import_default_model(_location(base), profile)

    assert _sha(profile / "config.yaml") == before


def test_later_failure_removes_the_imported_model_block(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})

    def failing_verifier(_profile_home) -> None:
        raise RuntimeError("hooks missing")

    with pytest.raises(Exception, match="hooks missing"):
        _installer(tmp_path, verifier=failing_verifier).install(start_gateway=False)

    assert "model" not in _profile_config(base)


def test_rollback_restores_a_preexisting_blank_model_entry(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    profile = base / "profiles" / PROFILE
    profile.mkdir(parents=True)
    config = profile / "config.yaml"
    config.write_text("model: {}\nother: 1\n", encoding="utf-8")

    change = import_default_model(_location(base), profile)
    assert change.changed and yaml.safe_load(config.read_text("utf-8"))["model"] == FAKE_MODEL
    assert yaml.safe_load(config.read_text("utf-8"))["other"] == 1
    change.rollback()

    assert yaml.safe_load(config.read_text("utf-8")) == {"model": {}, "other": 1}


def test_provider_plugin_enabled_in_base_but_not_in_profile_gets_an_install_notice(tmp_path: Path) -> None:
    base = _base(
        tmp_path,
        {"model": {"provider": "example-plugin-provider", "default": "example-model"},
         "plugins": {"enabled": ["example-plugin-provider"]}},
    )

    result = _installer(tmp_path).install(start_gateway=False)

    assert _profile_config(base)["model"]["provider"] == "example-plugin-provider"
    assert (
        f"hermes --profile {PROFILE} plugins install example-plugin-provider --enable"
        in " ".join(result.model_import.messages)
    )
    assert "example-plugin-provider" not in _profile_config(base)["plugins"]["enabled"]


def test_no_plugin_notice_for_a_provider_that_is_not_a_base_plugin(tmp_path: Path) -> None:
    _base(tmp_path, {"model": dict(FAKE_MODEL), "plugins": {"enabled": ["something-else"]}})

    result = _installer(tmp_path).install(start_gateway=False)

    assert not any("plugins install" in m for m in result.model_import.messages)


def test_credential_files_are_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    (base / ".env").write_text("OPENAI_API_KEY=sk-fake-secret\n", encoding="utf-8")
    (base / "auth.json").write_text('{"token": "tok-fake"}', encoding="utf-8")
    opened: list[str] = []
    real_open, real_read_text = builtins.open, Path.read_text

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def tracking_read_text(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    import_default_model(_location(base), base / "profiles" / PROFILE)

    names = {Path(p).name for p in opened}
    assert names == {"config.yaml"}
    assert not (base / "profiles" / PROFILE / ".env").exists()
    assert not (base / "profiles" / PROFILE / "auth.json").exists()


def test_uninstall_leaves_the_model_block_in_place(tmp_path: Path) -> None:
    base = _base(tmp_path, {"model": dict(FAKE_MODEL)})
    installer = _installer(tmp_path)
    installer.install(start_gateway=False)

    installer.uninstall()

    assert _profile_config(base)["model"] == FAKE_MODEL


def test_uninstall_does_not_touch_a_model_it_did_not_write(tmp_path: Path) -> None:
    base = _base(tmp_path)
    installer = _installer(tmp_path)
    installer.install(start_gateway=False)
    config = base / "profiles" / PROFILE / "config.yaml"
    data = yaml.safe_load(config.read_text("utf-8"))
    data["model"] = {"provider": "own-provider", "default": "own-model"}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    installer.uninstall()

    assert _profile_config(base)["model"] == {"provider": "own-provider", "default": "own-model"}
