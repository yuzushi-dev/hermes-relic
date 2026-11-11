"""Install the packaged RELIC Gumi directory plugin into a Hermes profile."""

from __future__ import annotations

import hashlib
import json
import importlib
import os
import shutil
import subprocess
import sys
import re
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import yaml
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME
from relic.hermes_plugin.lifecycle import LifecycleState, LifecycleStore, write_private_json
from relic.hermes_plugin.managed_cron import CronReconciliation, ManagedCron, ManagedCronError


PLUGIN_NAME = "relic-gumi"
PROFILE_NAME = HERMES_USER_PROFILE_NAME
STATE_FILENAME = ".relic-gumi-installer.json"
RUNTIME_FINGERPRINT_FILENAME = ".relic-runtime.sha256"

PROFILE_TEMPLATES = {
    "SOUL.md": "# Gumi\n\nBe warm, attentive, and concise.\n",
    "USER.md": "# User\n\nKeep personal preferences here.\n",
    "MEMORY.md": "# Memory\n\nKeep durable notes here.\n",
}
REQUIRED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "transform_llm_output",
    "pre_gateway_dispatch",
)
PACKAGE_REQUIREMENTS = {
    "jsonschema": "jsonschema>=4.0",
    "pydantic": "pydantic>=2.0",
    "yaml": "PyYAML>=6.0",
    "structlog": "structlog>=24.0",
}
HERMES_REQUIREMENT = "hermes-agent>=0.19.1"
COMMAND_TIMEOUT_SECONDS = 60
# Preparing a new Hermes environment generation downloads and installs packages.
PM_SYNC_TIMEOUT_SECONDS = 600
VENDOR_DIRNAME = "_vendor"
# Never copied into the plugin's private copy of the package: tests, caches, and the
# plugin payload itself (it is the directory the copy lands in).
_VENDOR_SKIPPED_DIRS = ("tests", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_VENDOR_SKIPPED_RELATIVE = ("hermes_plugin/gumi_entry",)
_GATEWAY_STATE_LINE = re.compile(
    r"^\s*(?:(?:gateway|service)\s*(?:status|state)?|(?:status|state))?\s*[:=]?\s*"
    r"(?P<state>absent|not installed|no service|stopped|not running|inactive|dead|"
    r"healthy|running|active|stale|zombie|conflict)\s*$",
    re.IGNORECASE,
)
_HERMES_GATEWAY_SERVICE_LINE = re.compile(
    r"^\s*[^\w\s]?\s*User gateway service is (?P<state>running|stopped)\s*$",
    re.IGNORECASE,
)
_HERMES_GATEWAY_IS_LINE = re.compile(
    r"^\s*[^\w\s]?\s*Gateway(?: service)? is "
    r"(?P<state>not running|not installed|running)(?:\s+(?:\([^)]*\)|via\b.*))?\s*$",
    re.IGNORECASE,
)
_HERMES_GATEWAY_INCOMPATIBLE_LINE = re.compile(
    r"^\s*[^\w\s]?\s*(?:Gateway is running\b.*|The running gateway)\s+but cannot serve\b.*$",
    re.IGNORECASE,
)
_SYSTEMD_ACTIVE_LINE = re.compile(
    r"^\s*Active:\s*(?P<state>active|inactive|failed)(?:\s+\([^)]*\))?.*$",
    re.IGNORECASE,
)


class InstallerError(RuntimeError):
    """Base error for a safe, incomplete installation."""


class HermesDiscoveryError(InstallerError):
    pass


class ForeignProfileError(InstallerError):
    """The Hermes profile exists but was not created by this installer."""


class PrerequisiteError(InstallerError):
    pass


class PluginVerificationError(InstallerError):
    pass


class GatewayError(InstallerError):
    pass


class GatewayConflictError(GatewayError):
    pass


class GatewayState(str, Enum):
    ABSENT = "absent"
    STOPPED = "stopped"
    HEALTHY = "healthy"
    STALE = "stale"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class HermesLocation:
    base_home: Path
    profile_home: Path
    source: str


@dataclass
class DirectoryPluginChange:
    path: Path
    changed: bool
    backup_path: Path | None = None

    def commit(self) -> None:
        if self.backup_path is not None:
            _remove_path(self.backup_path)
            self.backup_path = None

    def rollback(self) -> None:
        if not self.changed:
            return
        _remove_path(self.path)
        if self.backup_path is not None and self.backup_path.exists():
            os.replace(self.backup_path, self.path)
            self.backup_path = None


@dataclass
class ConfigChange:
    path: Path
    plugin_name: str
    changed: bool
    plugins_existed: bool
    enabled_existed: bool
    disabled_existed: bool
    enabled_before: bool
    disabled_before: bool
    entries_existed: bool = False
    entry_existed: bool = False
    entry_before: Any = None
    cron_changed: bool = False
    cron_existed: bool = False
    wrap_response_existed: bool = False
    wrap_response_before: Any = None

    def rollback(self) -> None:
        if not self.changed:
            return
        config = _load_config(self.path)
        plugins = config.get("plugins")
        if not isinstance(plugins, dict):
            return
        if self.entries_existed or self.entry_existed:
            entries = plugins.get("entries", {})
            if not isinstance(entries, dict):
                return
        else:
            entries = None
        enabled = _plugin_list(plugins, "enabled")
        disabled = _plugin_list(plugins, "disabled")
        _set_membership(enabled, self.plugin_name, self.enabled_before)
        _set_membership(disabled, self.plugin_name, self.disabled_before)
        if self.enabled_existed or enabled:
            plugins["enabled"] = sorted(enabled)
        else:
            plugins.pop("enabled", None)
        if self.disabled_existed or disabled:
            plugins["disabled"] = sorted(disabled)
        else:
            plugins.pop("disabled", None)
        if entries is not None:
            if self.entry_existed:
                entries[self.plugin_name] = deepcopy(self.entry_before)
            else:
                entries.pop(self.plugin_name, None)
            if self.entries_existed or entries:
                plugins["entries"] = entries
            else:
                plugins.pop("entries", None)
        if not self.plugins_existed and not plugins:
            config.pop("plugins", None)
        if self.cron_changed:
            cron = config.get("cron")
            if not isinstance(cron, dict):
                cron = {}
            if self.wrap_response_existed:
                cron["wrap_response"] = self.wrap_response_before
            else:
                cron.pop("wrap_response", None)
            if self.cron_existed or cron:
                config["cron"] = cron
            else:
                config.pop("cron", None)
        _write_config(self.path, config)



MODEL_IMPORT_KEYS = ("provider", "default")
MODEL_IMPORT_OPTIONAL_KEYS = ("api_mode", "base_url")


@dataclass
class ModelImportChange:
    """Outcome of importing the base config's default model into the profile config.

    ``rollback`` restores the profile's ``model`` entry exactly as it was before the
    import. Only non-credential keys are ever copied (see ``import_default_model``).
    """

    path: Path
    changed: bool = False
    model_existed: bool = False
    model_before: Any = None
    provider: str | None = None
    model: str | None = None
    messages: tuple[str, ...] = ()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        if not self.changed:
            return
        config = _load_config(self.path)
        if self.model_existed:
            config["model"] = deepcopy(self.model_before)
        else:
            config.pop("model", None)
        _write_config(self.path, config)


@dataclass
class PrivateFileChange:
    path: Path
    created: bool
    backup_path: Path | None = None

    def commit(self) -> None:
        if self.backup_path is not None:
            _remove_path(self.backup_path)
            self.backup_path = None

    def rollback(self) -> None:
        if self.created:
            _remove_path(self.path)
        elif self.backup_path is not None and self.backup_path.exists():
            _remove_path(self.path)
            os.replace(self.backup_path, self.path)
            self.backup_path = None


@dataclass
class TemplateChange:
    files: tuple[PrivateFileChange, ...]
    fingerprints: dict[str, str]

    def commit(self) -> None:
        for change in self.files:
            change.commit()

    def rollback(self) -> None:
        for change in reversed(self.files):
            change.rollback()

@dataclass
class LegacyPluginChange:
    artifacts: tuple[DirectoryPluginChange, ...] = ()
    configs: tuple[ConfigChange, ...] = ()

    def commit(self) -> None:
        for artifact in self.artifacts:
            artifact.commit()

    def rollback(self) -> None:
        for config in reversed(self.configs):
            config.rollback()
        for artifact in reversed(self.artifacts):
            artifact.rollback()

@dataclass(frozen=True)
class GatewayChange:
    previous: GatewayState
    actions: tuple[str, ...]


@dataclass(frozen=True)
class InstallResult:
    profile_home: Path
    plan: tuple[str, ...]
    plugin_changed: bool
    config_changed: bool
    gateway_actions: tuple[str, ...]
    model_import: ModelImportChange | None = None
    dependency_mode: str = "present"


@dataclass
class UninstallResult:
    profile_home: Path
    plugin_removed: bool
    config_changed: bool
    removed_jobs: tuple[str, ...]


Runner = Callable[..., CommandResult | tuple[int, str, str]]
Prompt = Callable[[str], bool]
Verifier = Callable[[Path], None]
CronChecker = Callable[[Path], None]
PackageChecker = Callable[[str], bool]
Which = Callable[[str], str | None]
InitializeUser = Callable[[Path], Any]
DeliveryArmed = Callable[[Path], bool]
ManagedCronFactory = Callable[
    [Path, Callable[[tuple[str, ...]], Any], Path], ManagedCron
]




def discover_hermes_home(
    hermes_home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> HermesLocation:
    """Resolve the one canonical Gumi profile with deterministic precedence."""
    environment = os.environ if environ is None else environ
    if hermes_home is not None:
        base_home = _resolved_path(hermes_home)
        if not base_home.is_dir():
            raise HermesDiscoveryError(f"Hermes home does not exist: {base_home}")
        return HermesLocation(base_home, base_home / "profiles" / PROFILE_NAME, "argument")

    configured_home = environment.get("HERMES_HOME", "").strip()
    configured_profiles = environment.get("HERMES_PROFILES_HOME", "").strip()
    if configured_home and configured_profiles:
        base_home = _resolved_path(configured_home)
        profiles_home = _resolved_path(configured_profiles)
        if profiles_home != base_home / "profiles":
            raise HermesDiscoveryError(
                "Ambiguous Hermes homes; pass --hermes-home <HERMES_HOME>."
            )
        if not base_home.is_dir():
            raise HermesDiscoveryError(f"Hermes home does not exist: {base_home}")
        if not profiles_home.is_dir():
            raise HermesDiscoveryError(f"Hermes profiles home does not exist: {profiles_home}")
        return HermesLocation(base_home, profiles_home / PROFILE_NAME, "HERMES_HOME")
    if configured_home:
        base_home = _resolved_path(configured_home)
        if not base_home.is_dir():
            raise HermesDiscoveryError(f"Hermes home does not exist: {base_home}")
        return HermesLocation(base_home, base_home / "profiles" / PROFILE_NAME, "HERMES_HOME")
    if configured_profiles:
        profiles_home = _resolved_path(configured_profiles)
        if profiles_home.name != "profiles":
            raise HermesDiscoveryError(
                "HERMES_PROFILES_HOME must end in 'profiles'; pass --hermes-home <HERMES_HOME>."
            )
        if not profiles_home.is_dir():
            raise HermesDiscoveryError(f"Hermes profiles home does not exist: {profiles_home}")
        return HermesLocation(profiles_home.parent, profiles_home / PROFILE_NAME, "HERMES_PROFILES_HOME")
    base_home = (home or Path.home()).expanduser().resolve() / ".hermes"
    return HermesLocation(base_home, base_home / "profiles" / PROFILE_NAME, "default")


def ensure_profile_is_installer_owned(profile_home: Path) -> None:
    """Refuse, before any write, a profile directory this installer did not create.

    The profile is the installer's when it does not exist, is an empty directory,
    or already carries one of the installer's own markers (``STATE_FILENAME`` after
    a completed run, ``.relic-gumi/lifecycle.json`` written first by
    ``LifecycleStore.begin()``, so a run that failed halfway stays re-runnable).
    Anything else, and any symlink, belongs to someone else and is left untouched.
    Reads only directory entries and marker names, never file contents.
    """
    if profile_home.is_symlink():
        raise ForeignProfileError(
            f"Hermes profile {profile_home} is a symbolic link, so it was not created by "
            "the relic-gumi installer. Nothing was modified. Use another --hermes-home, "
            "or remove or rename that profile yourself and run the command again."
        )
    if not profile_home.exists():
        return
    if not profile_home.is_dir():
        raise ForeignProfileError(
            f"Hermes profile path {profile_home} exists but is not a directory, so it was "
            "not created by the relic-gumi installer. Nothing was modified. Use another "
            "--hermes-home, or remove or rename it yourself and run the command again."
        )
    markers = (
        profile_home / STATE_FILENAME,
        profile_home / ".relic-gumi" / "lifecycle.json",
    )
    if any(marker.is_file() and not marker.is_symlink() for marker in markers):
        return
    try:
        empty = next(profile_home.iterdir(), None) is None
    except OSError as exc:
        raise ForeignProfileError(
            f"Hermes profile {profile_home} cannot be inspected ({exc.strerror or 'I/O error'}); "
            "nothing was modified."
        ) from exc
    if empty:
        return
    raise ForeignProfileError(
        f"Hermes profile {profile_home} already exists and was not created by the "
        "relic-gumi installer, so it was not modified. Use another --hermes-home, or "
        "remove or rename that profile yourself and run the command again."
    )


def plugin_artifact_source() -> Path:
    """Return the wheel-contained canonical Gumi directory-plugin payload."""
    source = Path(__file__).resolve().parent / "gumi_entry"
    if not (source / "plugin.yaml").is_file() or not (source / "__init__.py").is_file():
        raise InstallerError("relic-gumi package is missing its canonical directory-plugin artifact")
    return source


def install_directory_plugin(
    profile_home: Path,
    *,
    source: Path | None = None,
) -> DirectoryPluginChange:
    """Atomically replace only RELIC's directory plugin and retain a rollback backup."""
    source = plugin_artifact_source() if source is None else source
    _directory_fingerprint(source)
    target = profile_home / "plugins" / PLUGIN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{PLUGIN_NAME}.stage-{uuid.uuid4().hex}"
    backup = target.parent / f".{PLUGIN_NAME}.backup-{uuid.uuid4().hex}"
    moved_existing = False
    try:
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        runtime = stage / VENDOR_DIRNAME / "relic"
        # Exclude the entire output profile, including staging/backup siblings, if
        # it lives beneath the source package. Never copy profile data or recurse
        # into the copy currently being built.
        _vendor_runtime(Path(__file__).resolve().parents[1], runtime, profile_home)
        runtime_fingerprint = _directory_fingerprint(runtime)
        (stage / RUNTIME_FINGERPRINT_FILENAME).write_text(
            f"{runtime_fingerprint}\n", encoding="utf-8"
        )
        staged_fingerprint = _directory_fingerprint(stage)
        if target.is_dir() and not target.is_symlink():
            try:
                if _directory_fingerprint(target) == staged_fingerprint:
                    _remove_path(stage)
                    return DirectoryPluginChange(path=target, changed=False)
            except (InstallerError, OSError):
                pass
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
            moved_existing = True
        os.replace(stage, target)
    except Exception:
        _remove_path(stage)
        if moved_existing and backup.exists():
            _remove_path(target)
            os.replace(backup, target)
        raise
    return DirectoryPluginChange(path=target, changed=True, backup_path=backup if moved_existing else None)


def _vendor_runtime(runtime_root: Path, destination: Path, excluded_profile: Path) -> None:
    """Copy runtime files, pruning caches, tests, payload and output profile.

    Reject links rather than following them outside the package (or into loops).
    Build the bundle at install time; never keep a second runtime in the repo.
    """
    root = runtime_root.absolute()
    excluded = excluded_profile.absolute()
    skipped_relative = {Path(entry) for entry in _VENDOR_SKIPPED_RELATIVE}

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).absolute()
        skipped = set(shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")(directory, names))
        for name in names:
            child = current / name
            relative = child.relative_to(root)
            if (
                child == excluded or excluded in child.parents
                or name in _VENDOR_SKIPPED_DIRS or relative in skipped_relative
            ):
                skipped.add(name)
            if name not in skipped and (child.is_symlink() or not (child.is_dir() or child.is_file())):
                raise InstallerError(f"Invalid runtime artifact entry: {child}")
        return skipped

    shutil.copytree(root, destination, ignore=ignore)


def remove_directory_plugin(profile_home: Path) -> DirectoryPluginChange:
    """Remove the copied directory plugin, keeping a rollback backup."""
    target = profile_home / "plugins" / PLUGIN_NAME
    if not target.exists() and not target.is_symlink():
        return DirectoryPluginChange(path=target, changed=False)
    backup = target.parent / f".{PLUGIN_NAME}.backup-{uuid.uuid4().hex}"
    os.replace(target, backup)
    return DirectoryPluginChange(path=target, changed=True, backup_path=backup)


def disable_relic_gumi_plugin(profile_home: Path) -> ConfigChange:
    """Remove relic-gumi from the profile's enabled plugin list."""
    path = profile_home / "config.yaml"
    config = _load_config(path)
    plugins_existed = "plugins" in config
    if plugins_existed and not isinstance(config["plugins"], dict):
        raise InstallerError(f"Expected plugins mapping at {path}")
    plugins = config.get("plugins", {})
    enabled_existed = "enabled" in plugins
    disabled_existed = "disabled" in plugins
    enabled = _plugin_list(plugins, "enabled")
    disabled = _plugin_list(plugins, "disabled")
    enabled_before = PLUGIN_NAME in enabled
    disabled_before = PLUGIN_NAME in disabled
    changed = enabled_before
    if changed:
        _set_membership(enabled, PLUGIN_NAME, False)
        if enabled_existed or enabled:
            plugins["enabled"] = sorted(enabled)
        else:
            plugins.pop("enabled", None)
        config["plugins"] = plugins
        _write_config(path, config)
    return ConfigChange(
        path=path,
        plugin_name=PLUGIN_NAME,
        changed=changed,
        plugins_existed=plugins_existed,
        enabled_existed=enabled_existed,
        disabled_existed=disabled_existed,
        enabled_before=enabled_before,
        disabled_before=disabled_before,
    )


def enable_relic_gumi_plugin(profile_home: Path) -> ConfigChange:
    """Enable RELIC and default cron delivery to clean user-facing output."""
    path = profile_home / "config.yaml"
    config = _load_config(path)
    plugins_existed = "plugins" in config
    if plugins_existed and not isinstance(config["plugins"], dict):
        raise InstallerError(f"Expected plugins mapping at {path}")
    cron_existed = "cron" in config
    if cron_existed and not isinstance(config["cron"], dict):
        raise InstallerError(f"Expected cron mapping at {path}")
    plugins = config.setdefault("plugins", {})
    cron = config.setdefault("cron", {})
    enabled_existed = "enabled" in plugins
    disabled_existed = "disabled" in plugins
    enabled = _plugin_list(plugins, "enabled")
    disabled = _plugin_list(plugins, "disabled")
    enabled_before = PLUGIN_NAME in enabled
    disabled_before = PLUGIN_NAME in disabled
    wrap_response_existed = "wrap_response" in cron
    wrap_response_before = cron.get("wrap_response")
    cron_changed = not wrap_response_existed
    if cron_changed:
        cron["wrap_response"] = False
    plugin_changed = not enabled_before or disabled_before
    if plugin_changed:
        _set_membership(enabled, PLUGIN_NAME, True)
        _set_membership(disabled, PLUGIN_NAME, False)
        plugins["enabled"] = sorted(enabled)
        if disabled_existed or disabled:
            plugins["disabled"] = sorted(disabled)
        else:
            plugins.pop("disabled", None)
    changed = plugin_changed or cron_changed
    if changed:
        _write_config(path, config)
    return ConfigChange(
        path=path,
        plugin_name=PLUGIN_NAME,
        changed=changed,
        plugins_existed=plugins_existed,
        enabled_existed=enabled_existed,
        disabled_existed=disabled_existed,
        enabled_before=enabled_before,
        disabled_before=disabled_before,
        cron_changed=cron_changed,
        cron_existed=cron_existed,
        wrap_response_existed=wrap_response_existed,
        wrap_response_before=wrap_response_before,
    )

def _enabled_plugin_names(config: Mapping[str, Any]) -> set[str]:
    plugins = config.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list):
        return set()
    return {item for item in enabled if isinstance(item, str)}


def _usable_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def import_default_model(location: HermesLocation, profile_home: Path) -> ModelImportChange:
    """Copy the base config's default provider and model into a profile that has none.

    Reads only ``<base_home>/config.yaml`` and the profile's ``config.yaml``: never
    ``.env``, ``auth.json`` or credential pools. Copies ``model.provider`` and
    ``model.default`` plus ``api_mode`` and ``base_url`` when present; every other key
    (for instance an ``api_key``) is left behind, and a ``base_url`` carrying userinfo
    or a query string is dropped. A profile that already has ``model.provider`` or
    ``model.default`` is never touched. Writes nothing when there is nothing to import.
    Call it only after ``ensure_profile_is_installer_owned``.
    """
    path = profile_home / "config.yaml"
    base_path = location.base_home / "config.yaml"
    base = _load_config(base_path)
    profile = _load_config(path)

    profile_model = profile.get("model")
    if isinstance(profile_model, str) and profile_model.strip():
        return ModelImportChange(path=path)
    if isinstance(profile_model, dict):
        if _usable_text(profile_model.get("provider")) or _usable_text(profile_model.get("default")):
            return ModelImportChange(path=path)
    elif profile_model not in (None, ""):
        raise InstallerError(
            f"Expected model mapping at {path}, found {type(profile_model).__name__}"
        )

    set_by_hand = (
        f"hermes --profile {PROFILE_NAME} config set model.provider <provider>; "
        f"hermes --profile {PROFILE_NAME} config set model.default <model-id>"
    )
    base_model = base.get("model")
    provider = _usable_text(base_model.get("provider")) if isinstance(base_model, dict) else None
    default = _usable_text(base_model.get("default")) if isinstance(base_model, dict) else None
    if provider is None or default is None:
        return ModelImportChange(
            path=path,
            messages=(
                "No default model to import: the base Hermes config has no usable "
                "model.provider and model.default. Set the profile's provider and model: "
                + set_by_hand,
            ),
        )

    imported: dict[str, str] = {"provider": provider, "default": default}
    messages: list[str] = []
    api_mode = _usable_text(base_model.get("api_mode"))
    if api_mode is not None:
        imported["api_mode"] = api_mode
    if "base_url" in base_model:
        base_url = _usable_text(base_model.get("base_url"))
        if base_url is not None and ("@" in base_url or "?" in base_url):
            messages.append(
                "The base model.base_url carries userinfo or a query string, so it was not "
                "imported; set it in the profile yourself if it is needed."
            )
        elif base_url is not None:
            imported["base_url"] = base_url

    model_existed = "model" in profile
    model_before = deepcopy(profile_model)
    merged = dict(profile_model) if isinstance(profile_model, dict) else {}
    for key, value in imported.items():
        if not _usable_text(merged.get(key)):
            merged[key] = value
    profile["model"] = merged
    _write_config(path, profile)

    messages.insert(0, f"Imported default model from the base Hermes config: provider {provider}, model {default}.")
    if provider in _enabled_plugin_names(base) and provider not in _enabled_plugin_names(profile):
        messages.append(
            f"The provider {provider} is a plugin enabled in the base config but not in the "
            f"profile. Install it there yourself: hermes --profile {PROFILE_NAME} plugins "
            f"install {provider} --enable"
        )
    return ModelImportChange(
        path=path,
        changed=True,
        model_existed=model_existed,
        model_before=model_before,
        provider=provider,
        model=default,
        messages=tuple(messages),
    )


def _migrate_legacy_base_plugin(location: HermesLocation) -> LegacyPluginChange:
    artifacts: list[DirectoryPluginChange] = []
    configs: list[ConfigChange] = []
    try:
        for plugin_name in ("relic", PLUGIN_NAME):
            path = location.base_home / "plugins" / plugin_name
            if path.exists() or path.is_symlink():
                if not _is_legacy_base_plugin(path):
                    raise InstallerError(f"Cannot replace an unrecognized plugin at {path}")
                backup = path.parent / f".{plugin_name}.legacy-{uuid.uuid4().hex}"
                os.replace(path, backup)
                artifacts.append(
                    DirectoryPluginChange(path=path, changed=True, backup_path=backup)
                )
            config = _disable_legacy_base_plugin(
                location.base_home / "config.yaml", plugin_name
            )
            if config is not None:
                configs.append(config)
    except Exception:
        LegacyPluginChange(tuple(artifacts), tuple(configs)).rollback()
        raise
    return LegacyPluginChange(tuple(artifacts), tuple(configs))


def _is_legacy_base_plugin(path: Path) -> bool:
    try:
        source = path.resolve(strict=False)
    except OSError:
        return False
    if source.parts[-3:] == ("relic", "hermes_plugin", "hermes_entry"):
        return True
    return (
        (source / "src" / "relic").is_dir()
        and (source / "plugin.yaml").is_file()
        and (source / "__init__.py").is_file()
    )


def _disable_legacy_base_plugin(
    path: Path, plugin_name: str
) -> ConfigChange | None:
    config = _load_config(path)
    if "plugins" not in config:
        return None
    plugins = config["plugins"]
    if not isinstance(plugins, dict):
        raise InstallerError(f"Expected plugins mapping at {path}")
    enabled_existed = "enabled" in plugins
    disabled_existed = "disabled" in plugins
    entries_existed = "entries" in plugins
    enabled = _plugin_list(plugins, "enabled")
    disabled = _plugin_list(plugins, "disabled")
    entries = plugins.get("entries", {})
    if not isinstance(entries, dict):
        raise InstallerError(f"Expected plugins.entries mapping at {path}")
    enabled_before = plugin_name in enabled
    disabled_before = plugin_name in disabled
    entry_existed = plugin_name in entries
    if not (enabled_before or disabled_before or entry_existed):
        return None
    entry_before = deepcopy(entries.get(plugin_name))
    _set_membership(enabled, plugin_name, False)
    _set_membership(disabled, plugin_name, False)
    if enabled_existed or enabled:
        plugins["enabled"] = sorted(enabled)
    else:
        plugins.pop("enabled", None)
    if disabled_existed or disabled:
        plugins["disabled"] = sorted(disabled)
    else:
        plugins.pop("disabled", None)
    entries.pop(plugin_name, None)
    if entries_existed or entries:
        plugins["entries"] = entries
    else:
        plugins.pop("entries", None)
    if not plugins:
        config.pop("plugins", None)
    _write_config(path, config)
    return ConfigChange(
        path=path,
        plugin_name=plugin_name,
        changed=True,
        plugins_existed=True,
        enabled_existed=enabled_existed,
        disabled_existed=disabled_existed,
        enabled_before=enabled_before,
        disabled_before=disabled_before,
        entries_existed=entries_existed,
        entry_existed=entry_existed,
        entry_before=entry_before,
    )




def gateway_plan(state: GatewayState, *, changed: bool) -> tuple[str, ...]:
    if state is GatewayState.CONFLICT:
        raise GatewayConflictError("Gateway state conflicts with RELIC ownership; refusing outbound work.")
    if state is GatewayState.ABSENT:
        return ("install", "start")
    if state is GatewayState.STOPPED:
        return ("start",)
    if state is GatewayState.STALE:
        return ("repair", "start")
    if state is GatewayState.HEALTHY:
        return ("restart",) if changed else ()
    raise GatewayConflictError("Gateway state is unknown; refusing outbound work.")


@dataclass
class Installer:
    """Injectable installer boundary: prompt and process execution are replaceable."""

    runner: Runner = field(default=lambda command, **kwargs: _run(command, **kwargs))
    prompt: Prompt = field(default=lambda question: _prompt(question))
    verifier: Verifier = field(default=lambda profile_home: verify_installed_plugin(profile_home))
    cron_checker: CronChecker | None = None
    package_available: PackageChecker = field(default=lambda module: _package_available(module))
    which: Which = field(default=shutil.which)
    initialize_user: InitializeUser = field(default=lambda profiles_home: _initialize_user(profiles_home))
    delivery_armed: DeliveryArmed = field(default=lambda base_home: _delivery_armed(base_home))
    managed_cron_factory: ManagedCronFactory = field(
        default=lambda base_home, runner, scripts_dir: ManagedCron(
            base_home=base_home, runner=runner, scripts_dir=scripts_dir
        )
    )
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    pm_managed: Callable[[], bool] = field(default=lambda: _running_in_pm_environment())

    def ensure_prerequisites(self, *, non_interactive: bool = False) -> str:
        """Check what this interpreter needs and return how dependencies are provided.

        ``"present"``: nothing was missing. ``"pip"``: an unmanaged Hermes, packages were
        installed into this interpreter with pip. ``"pm"``: this interpreter is one of
        Hermes' package-manager environments; packages are never installed into it by
        hand (the next environment generation would not keep them), and the plugin's own
        requirements are prepared by ``hermes plugins enable`` after the copy.
        """
        missing = [
            requirement
            for module, requirement in PACKAGE_REQUIREMENTS.items()
            if not self.package_available(module)
        ]
        if not self.package_available("hermes_cli"):
            missing.append(HERMES_REQUIREMENT)
        managed = bool(self.pm_managed())
        if not missing:
            return "pm" if managed else "present"
        if managed:
            raise PrerequisiteError(
                f"Missing prerequisites: {', '.join(missing)}. This interpreter belongs to "
                "Hermes' package manager, which rebuilds its environments, so the installer "
                "does not pip-install into it. Let Hermes prepare the plugin's dependencies "
                "(`hermes plugins install <path-to-this-repository> --yes-deps`, or the "
                "Plugins panel of the Desktop app), then run the installer with the "
                "interpreter of the new environment."
            )
        remediation = _remediation(missing)
        message = f"Missing prerequisites: {', '.join(missing)}. Remediation: {remediation}"
        if non_interactive:
            raise PrerequisiteError(message)
        if not self.prompt(f"Install missing prerequisites ({', '.join(missing)})?"):
            raise PrerequisiteError(message)
        result = _result(self.runner([sys.executable, "-m", "pip", "install", *missing]))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PrerequisiteError(f"{message}\nInstaller failed: {detail}")
        importlib.invalidate_caches()
        if any(not self.package_available(module) for module in PACKAGE_REQUIREMENTS) or (
            not self.package_available("hermes_cli")
        ):
            raise PrerequisiteError(message)
        return "pip"

    def _sync_plugin_dependencies(self, location: HermesLocation) -> None:
        """Ask Hermes' package manager to prepare the plugin's declared requirements.

        ``hermes plugins enable`` on an already enabled plugin still resolves the
        ``python_dependencies`` of the copied ``plugin.yaml`` into a new environment
        generation (verified against Hermes 0.23, see docs/hermes-integration.md).
        """
        executable = self.which("hermes")
        command = (
            [executable, "--profile", PROFILE_NAME, "plugins", "enable", PLUGIN_NAME]
            if executable
            else self._command("plugins", "enable", PLUGIN_NAME)
        )
        result = _result(
            self.runner(
                command,
                env=self._environment(location),
                timeout=PM_SYNC_TIMEOUT_SECONDS,
            )
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise InstallerError(
                "Hermes could not prepare the plugin's Python dependencies"
                + (f": {detail}" if detail else ".")
                + f" Run `hermes --profile {PROFILE_NAME} plugins enable {PLUGIN_NAME}` "
                "once the cause is fixed, then run the installer again."
            )

    def install(
        self,
        *,
        hermes_home: Path | str | None = None,
        non_interactive: bool = False,
        start_gateway: bool = True,
    ) -> InstallResult:
        location = discover_hermes_home(hermes_home, environ=self.environ, home=self.home)
        # Before lifecycle.begin(): a profile that is not ours must not receive any write.
        ensure_profile_is_installer_owned(location.profile_home)

        lifecycle = LifecycleStore(location.profile_home)
        plugin_change: DirectoryPluginChange | None = None
        config_change: ConfigChange | None = None
        model_change: ModelImportChange | None = None
        template_change: TemplateChange | None = None
        legacy_change: LegacyPluginChange | None = None
        gateway_change: GatewayChange | None = None
        managed_cron: ManagedCron | None = None
        cron_change: CronReconciliation | None = None
        try:
            lifecycle.begin()
            dependency_mode = self.ensure_prerequisites(non_interactive=non_interactive) or "present"
            self.initialize_user(location.base_home / "profiles")
            _initialize_database(_relic_home(self.environ, self.home) / "relic.db")
            legacy_change = _migrate_legacy_base_plugin(location)
            plugin_change = install_directory_plugin(location.profile_home)
            lifecycle.advance(LifecycleState.INSTALLED)
            config_change = enable_relic_gumi_plugin(location.profile_home)
            model_change = import_default_model(location, location.profile_home)
            template_change = ensure_profile_templates(location.profile_home)
            if dependency_mode == "pm":
                self._sync_plugin_dependencies(location)
            lifecycle.advance(LifecycleState.ENABLED)
            try:
                self.verifier(location.profile_home)
            except Exception as exc:
                raise PluginVerificationError(str(exc)) from exc
            lifecycle.advance(LifecycleState.HOST_V2_LOADED)
            lifecycle.advance(LifecycleState.THREE_HOOKS_VERIFIED)

            changed = bool(
                plugin_change.changed
                or config_change.changed
                or model_change.changed
                or legacy_change.artifacts
                or legacy_change.configs
            )
            self._check_cron(location)
            if not start_gateway:
                previous = self._gateway_status(location)
                if previous not in {GatewayState.ABSENT, GatewayState.STOPPED}:
                    raise GatewayError(
                        "Gateway must be stopped before an offline reconciliation."
                    )
            managed_cron, cron_change = self._reconcile_managed_cron(location)
            gateway_change = (
                self._reconcile_gateway(location, changed=changed)
                if start_gateway
                else GatewayChange(previous=previous, actions=())
            )
            _write_state(
                location.profile_home,
                gateway_change,
                template_change.fingerprints,
            )
            if start_gateway:
                lifecycle.advance(LifecycleState.GATEWAY_READY)
            template_change.commit()
            plugin_change.commit()
            legacy_change.commit()
            plan = (
                "preflight",
                "dependencies",
                "plugin_installed" if plugin_change.changed else "plugin_unchanged",
                "plugin_enabled" if config_change.changed else "plugin_already_enabled",
                "hooks_verified",
                "cron_checked",
                "cron_reconciled",
                *(f"gateway_{action}" for action in gateway_change.actions),
                "delivery_eligible" if start_gateway else "gateway_deferred",
            )
            return InstallResult(
                profile_home=location.profile_home,
                plan=plan,
                plugin_changed=plugin_change.changed,
                config_changed=config_change.changed or model_change.changed,
                gateway_actions=gateway_change.actions,
                model_import=model_change,
                dependency_mode=dependency_mode,
            )
        except Exception as exc:
            try:
                lifecycle.fail(str(exc))
            except Exception:
                pass
            if managed_cron is not None and cron_change is not None:
                try:
                    managed_cron.rollback(cron_change)
                except Exception:
                    pass
            if gateway_change is not None:
                self._rollback_gateway(location, gateway_change)
            if template_change is not None:
                template_change.rollback()
            if model_change is not None:
                model_change.rollback()
            if config_change is not None:
                config_change.rollback()
            if plugin_change is not None:
                plugin_change.rollback()
            if legacy_change is not None:
                legacy_change.rollback()
            raise

    def uninstall(self, *, hermes_home: Path | str | None = None) -> UninstallResult:
        """Remove the owned cron jobs, deregister, and remove the copied plugin.

        Does not touch $RELIC_HOME/user/; that is the caller's decision.
        """
        location = discover_hermes_home(hermes_home, environ=self.environ, home=self.home)
        # Never remove jobs, config entries or plugin directories from a profile we did not create.
        ensure_profile_is_installer_owned(location.profile_home)
        relic_home = _relic_home(self.environ, self.home)
        manager = self.managed_cron_factory(
            relic_home,
            lambda command: self.runner(
                self._command(*command), env=self._environment(location)
            ),
            location.profile_home / "scripts",
        )
        if isinstance(manager, ManagedCron):
            manager.configure_hermes_jobs_path(location.profile_home)
        try:
            removed_jobs = manager.remove_all()
        except ManagedCronError as exc:
            raise InstallerError(str(exc)) from exc

        config_change: ConfigChange | None = None
        plugin_change: DirectoryPluginChange | None = None
        try:
            config_change = disable_relic_gumi_plugin(location.profile_home)
            plugin_change = remove_directory_plugin(location.profile_home)
        except Exception:
            if plugin_change is not None:
                plugin_change.rollback()
            if config_change is not None:
                config_change.rollback()
            raise
        plugin_change.commit()

        return UninstallResult(
            profile_home=location.profile_home,
            plugin_removed=plugin_change.changed,
            config_changed=config_change.changed,
            removed_jobs=removed_jobs,
        )

    def check_managed_cron(self, *, hermes_home: Path | str | None = None) -> tuple[str, ...]:
        """Read-only: report each owned cron job's reconciliation state."""
        location = discover_hermes_home(hermes_home, environ=self.environ, home=self.home)
        relic_home = _relic_home(self.environ, self.home)
        manager = self.managed_cron_factory(
            relic_home,
            lambda command: self.runner(
                self._command(*command), env=self._environment(location)
            ),
            location.profile_home / "scripts",
        )
        if isinstance(manager, ManagedCron):
            manager.configure_hermes_jobs_path(location.profile_home)
        try:
            return manager.check_owned()
        except ManagedCronError as exc:
            raise InstallerError(str(exc)) from exc

    def _command(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "hermes_cli.main", "--profile", PROFILE_NAME, *args]

    def _environment(self, location: HermesLocation) -> dict[str, str]:
        environment = dict(self.environ)
        environment["HERMES_HOME"] = str(location.base_home)
        return environment

    def _reconcile_gateway(self, location: HermesLocation, *, changed: bool) -> GatewayChange:
        previous = self._gateway_status(location)
        actions = gateway_plan(previous, changed=changed)
        for action in actions:
            command = {
                "install": self._command("gateway", "install"),
                "start": self._command("gateway", "start"),
                "restart": self._command("gateway", "restart"),
                "repair": self._command("gateway", "install", "--force"),
            }[action]
            result = _result(self.runner(command, env=self._environment(location)))
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise GatewayError(f"Gateway {action} failed: {detail}")
        if actions:
            final_status = _result(
                self.runner(self._command("gateway", "status"), env=self._environment(location))
            )
            if _gateway_state(final_status) is not GatewayState.HEALTHY:
                raise GatewayError("Gateway did not become healthy; outbound work remains blocked.")
        return GatewayChange(previous=previous, actions=actions)

    def _gateway_status(self, location: HermesLocation) -> GatewayState:
        return _gateway_state(
            _result(
                self.runner(
                    self._command("gateway", "status"),
                    env=self._environment(location),
                )
            )
        )

    def _check_cron(self, location: HermesLocation) -> None:
        if self.cron_checker is not None:
            self.cron_checker(location.profile_home)
            return
        result = _result(
            self.runner(self._command("cron", "status"), env=self._environment(location))
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise InstallerError(f"Cron check failed: {detail}")

    def _reconcile_managed_cron(
        self, location: HermesLocation
    ) -> tuple[ManagedCron, CronReconciliation]:
        relic_home = _relic_home(self.environ, self.home)
        try:
            manager = self.managed_cron_factory(
                relic_home,
                lambda command: self.runner(
                    self._command(*command), env=self._environment(location)
                ),
                location.profile_home / "scripts",
            )
            if isinstance(manager, ManagedCron):
                manager.configure_hermes_jobs_path(location.profile_home)
            change = manager.reconcile(delivery_armed=self.delivery_armed(relic_home))
        except ManagedCronError as exc:
            raise InstallerError(str(exc)) from exc
        return manager, change

    def _rollback_gateway(self, location: HermesLocation, change: GatewayChange) -> None:
        try:
            if change.previous is GatewayState.ABSENT and "install" in change.actions:
                self.runner(self._command("gateway", "uninstall"), env=self._environment(location))
            elif change.previous is GatewayState.STOPPED and "start" in change.actions:
                self.runner(self._command("gateway", "stop"), env=self._environment(location))
        except Exception:
            pass


def verify_installed_plugin(profile_home: Path) -> None:
    """Load the installed directory plugin and exercise every owned hook."""
    plugin_dir = profile_home / "plugins" / PLUGIN_NAME
    expected_module = (plugin_dir / "__init__.py").resolve()
    with _environment(HERMES_HOME=str(profile_home), HERMES_ENABLE_PROJECT_PLUGINS=None):
        try:
            from hermes_cli.plugins import PluginManager
        except ImportError as exc:
            raise PluginVerificationError(
                "relic-gumi verification requires Hermes in this Python interpreter"
            ) from exc
        manager = PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins.get(PLUGIN_NAME)
        if loaded is None:
            raise PluginVerificationError("relic-gumi plugin was not discovered")
        if not loaded.enabled or loaded.error is not None:
            raise PluginVerificationError(loaded.error or "relic-gumi plugin is disabled")
        manifest = getattr(loaded, "manifest", None)
        manifest_path = getattr(manifest, "path", None)
        module_path = getattr(loaded.module, "__file__", None)
        if (
            getattr(manifest, "source", None) != "user"
            or not isinstance(manifest_path, str)
            or Path(manifest_path).resolve() != plugin_dir.resolve()
            or not isinstance(module_path, str)
            or Path(module_path).resolve() != expected_module
        ):
            raise PluginVerificationError(
                "relic-gumi was not loaded from the profile plugin directory"
            )
        if set(loaded.hooks_registered) != set(REQUIRED_HOOKS):
            raise PluginVerificationError("relic-gumi did not register all required hooks")
        callbacks = _owned_callbacks(manager, getattr(loaded.module, "__name__", ""))
        # pre_gateway_dispatch only runs on real inbound messages: registered and
        # callable is all that can be checked without side effects.
        if not callable(callbacks["pre_gateway_dispatch"]):
            raise PluginVerificationError("pre_gateway_dispatch callback is not callable")
        verification = {"execution_context": "background", "user_visible": False}
        pre_result = callbacks["pre_llm_call"](
            **verification,
            session_id="relic-gumi-verification",
            user_message="",
        )
        if pre_result is not None and (
            not isinstance(pre_result, dict) or not isinstance(pre_result.get("context"), str)
        ):
            raise PluginVerificationError("pre_llm_call returned an invalid result")
        post_result = callbacks["post_llm_call"](
            **verification,
            user_message="",
            assistant_response="",
        )
        if post_result is not None:
            raise PluginVerificationError("post_llm_call returned an invalid result")
        transformed = callbacks["transform_llm_output"](
            **verification,
            response_text="relic-gumi verification",
        )
        if transformed is not None and not isinstance(transformed, str):
            raise PluginVerificationError("transform_llm_output returned an invalid result")
        if not plugin_dir.is_dir():
            raise PluginVerificationError("relic-gumi was not loaded from the profile plugin directory")


def _owned_callbacks(manager: Any, owner_module: str) -> dict[str, Callable[..., Any]]:
    if not owner_module:
        raise PluginVerificationError("relic-gumi did not provide a loaded module")
    callbacks: dict[str, Callable[..., Any]] = {}
    for hook_name in REQUIRED_HOOKS:
        owned = [
            callback
            for callback in manager._hooks.get(hook_name, ())
            if getattr(callback, "__module__", None) == owner_module
        ]
        if len(owned) != 1:
            raise PluginVerificationError(
                f"relic-gumi expected one owned {hook_name} callback; found {len(owned)}"
            )
        callbacks[hook_name] = owned[0]
    return callbacks


def _gateway_state(result: CommandResult) -> GatewayState:
    states: set[GatewayState] = set()
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        if _HERMES_GATEWAY_INCOMPATIBLE_LINE.fullmatch(line):
            states.add(GatewayState.STALE)
            continue
        match = (
            _GATEWAY_STATE_LINE.fullmatch(line)
            or _HERMES_GATEWAY_SERVICE_LINE.fullmatch(line)
            or _HERMES_GATEWAY_IS_LINE.fullmatch(line)
            or _SYSTEMD_ACTIVE_LINE.fullmatch(line)
        )
        if match is None:
            continue
        value = match.group("state").lower()
        if value in {"absent", "not installed", "no service"}:
            states.add(GatewayState.ABSENT)
        elif value in {"stopped", "not running", "inactive", "dead"}:
            states.add(GatewayState.STOPPED)
        elif value in {"healthy", "running", "active"}:
            states.add(GatewayState.HEALTHY)
        elif value in {"stale", "zombie", "failed"}:
            states.add(GatewayState.STALE)
        else:
            states.add(GatewayState.CONFLICT)
    if len(states) == 1:
        return states.pop()
    if len(states) > 1:
        return GatewayState.CONFLICT
    return GatewayState.HEALTHY if result.returncode == 0 else GatewayState.CONFLICT


def _write_state(
    profile_home: Path,
    gateway_change: GatewayChange,
    template_fingerprints: Mapping[str, str],
) -> None:
    plugin_path = profile_home / "plugins" / PLUGIN_NAME
    config_path = profile_home / "config.yaml"
    write_private_json(
        profile_home / STATE_FILENAME,
        {
            "schema_version": 1,
            "plugin": PLUGIN_NAME,
            "profile": PROFILE_NAME,
            "ownership": {
                "plugin_directory": f"plugins/{PLUGIN_NAME}",
                "config_entries": {"plugins.enabled": True, "plugins.disabled": False},
                "template_files": sorted(template_fingerprints),
            },
            "plugin_fingerprint": _directory_fingerprint(plugin_path),
            "config_fingerprint": _file_fingerprint(config_path),
            "gateway_actions": list(gateway_change.actions),
            "template_fingerprints": dict(sorted(template_fingerprints.items())),
        },
    )


def _initialize_user(profiles_home: Path) -> Any:
    from relic.profile.registry import ProfileRegistry

    return ProfileRegistry(hermes_profiles_home=profiles_home).initialize_user()


def _initialize_database(db_path: Path) -> None:
    from relic.db import init_db

    init_db(db_path)


def _relic_home(environ: Mapping[str, str], home: Path | None) -> Path:
    configured = environ.get("RELIC_HOME", "").strip()
    if configured:
        return Path(configured)
    return (Path(home) if home is not None else Path.home()) / ".relic"


def _delivery_armed(relic_home: Path) -> bool:
    try:
        from relic.profile.user_onboarding import load_onboarding_state

        state = load_onboarding_state(relic_home / "user")
    except (OSError, ValueError):
        return False
    return bool(state and state.get("delivery_execution_armed") is True)


def _package_available(module: str) -> bool:
    return find_spec(module) is not None


def _running_in_pm_environment() -> bool:
    """True when this interpreter is one of Hermes' package-manager environments.

    PM lays environments out as ``<hermes root>/installs/<id>/environments/<id>/venv``.
    A plain pip-installed Hermes does not, and keeps the pip fallback.
    """
    prefix = Path(sys.prefix).absolute()
    parts = prefix.parts
    return (
        len(parts) >= 5
        and parts[-1] == "venv"
        and parts[-3] == "environments"
        and parts[-5] == "installs"
    )


def _prompt(question: str) -> bool:
    try:
        return input(f"{question} (y/N) ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> CommandResult:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=None if env is None else dict(env),
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _result(value: CommandResult | tuple[int, str, str]) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    return CommandResult(*value)


def _remediation(requirements: Sequence[str]) -> str:
    return " ".join([sys.executable, "-m", "pip", "install", *requirements])


def _resolved_path(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _directory_fingerprint(directory: Path, *, exclude: Path | None = None) -> str:
    digest = hashlib.sha256()
    if not directory.is_dir():
        raise InstallerError(f"Expected directory plugin at {directory}")
    excluded = exclude.absolute() if exclude is not None else None
    for path in sorted(directory.rglob("*")):
        absolute = path.absolute()
        relative = path.relative_to(directory)
        if excluded is not None and (absolute == excluded or excluded in absolute.parents):
            continue
        if path.is_dir() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or not path.is_file():
            raise InstallerError(f"Invalid plugin artifact entry: {path}")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_profile_templates(profile_home: Path) -> TemplateChange:
    """Create private profile files and update only files this installer owns."""
    if profile_home.is_symlink():
        raise InstallerError(f"Profile home must not be a symlink: {profile_home}")
    profile_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not profile_home.is_dir():
        raise InstallerError(f"Profile home is not a directory: {profile_home}")
    if os.name == "posix":
        profile_home.chmod(0o700)

    files: list[PrivateFileChange] = []
    environment = profile_home / ".env"
    if not environment.exists():
        files.append(_replace_private_text(environment, ""))
    else:
        _require_private_file(environment)

    owned = _owned_template_fingerprints(profile_home)
    fingerprints: dict[str, str] = {}
    for name, text in PROFILE_TEMPLATES.items():
        path = profile_home / name
        expected = _file_fingerprint_text(text)
        if not path.exists():
            files.append(_replace_private_text(path, text))
            fingerprints[name] = expected
            continue
        _require_private_file(path)
        current = _file_fingerprint(path)
        previous = owned.get(name)
        if previous != current:
            if previous is not None:
                fingerprints[name] = previous
            continue
        if current != expected:
            files.append(_replace_private_text(path, text))
        fingerprints[name] = expected
    return TemplateChange(tuple(files), fingerprints)


def _owned_template_fingerprints(profile_home: Path) -> dict[str, str]:
    path = profile_home / STATE_FILENAME
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    values = state.get("template_fingerprints") if isinstance(state, dict) else None
    if not isinstance(values, dict):
        return {}
    return {
        name: value
        for name, value in values.items()
        if name in PROFILE_TEMPLATES and isinstance(value, str)
    }


def _replace_private_text(path: Path, text: str) -> PrivateFileChange:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallerError(f"Private profile file is unsafe: {path}")
    backup: Path | None = None
    created = not path.exists()
    if not created:
        backup = path.parent / f".{path.name}.backup-{uuid.uuid4().hex}"
        os.replace(path, backup)
    try:
        _write_text_atomic(path, text)
    except Exception:
        if backup is not None and backup.exists():
            _remove_path(path)
            os.replace(backup, path)
        raise
    return PrivateFileChange(path=path, created=created, backup_path=backup)


def _require_private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise InstallerError(f"Private profile file is unsafe: {path}")
    if os.name == "posix":
        path.chmod(0o600)


def _file_fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InstallerError(f"{path} is not valid YAML; nothing was changed.") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InstallerError(f"Expected mapping at {path}, found {type(value).__name__}")
    return value


def _plugin_list(plugins: dict[str, Any], key: str) -> set[str]:
    value = plugins.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InstallerError(f"Expected plugins.{key} list")
    return set(value)


def _set_membership(values: set[str], value: str, present: bool) -> None:
    if present:
        values.add(value)
    else:
        values.discard(value)


def _write_config(path: Path, config: Mapping[str, Any]) -> None:
    _write_text_atomic(path, yaml.safe_dump(dict(config), sort_keys=False, allow_unicode=True))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    stage = path.parent / f".{path.name}.stage-{uuid.uuid4().hex}"
    try:
        with stage.open("w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), mode)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(stage, path)
    finally:
        _remove_path(stage)


@contextmanager
def _environment(**updates: str | None) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
