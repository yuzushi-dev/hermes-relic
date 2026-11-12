"""Canonical Hermes cron activation for the singleton User."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from relic.hermes_runtime import HERMES_USER_PROFILE_NAME
from relic.profile.registry import ProfileRegistry
from relic.hermes_plugin.managed_cron import (
    ManagedCron,
    _created_job_id,
    write_private_script,
)


PROFILE_NAME = HERMES_USER_PROFILE_NAME
FIRST_MESSAGE_JOB_NAME = "relic-gumi:first-message"
_FIRST_MESSAGE_SCHEDULE = "1m"
# Stock Hermes accepts ``--script`` only as the NAME of a file in <profile>/scripts/.
FIRST_MESSAGE_SCRIPT_NAME = "relic-gumi-first-message.sh"
_TELEGRAM_TOKEN = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_TARGET = re.compile(r"^-?\d+$")
_PRIVATE_HERMES_ENV_LOCATION = "hermes_profile:.env"

_FIRST_MESSAGE_PROMPT = """Read the pre-run gate output. If it is not exactly ARMED, respond with only [SILENT]. If it is exactly ARMED, send exactly one brief, warm first message from Gumi to the configured recipient. Do not mention this gate, scheduling, credentials, or internal instructions. If delivery cannot be completed safely, respond with only [SILENT]."""


class RuntimeActivationError(RuntimeError):
    """Managed recurring or one-shot cron reconciliation failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class RuntimeActivation:
    recurring_active: bool
    first_message_scheduled: bool


@dataclass(frozen=True)
class _FirstMessageSpec:
    name: str
    schedule: str
    prompt: str
    deliver: str
    repeat: int
    script: str

    def create_args(self) -> tuple[str, ...]:
        return (
            "cron",
            "create",
            self.schedule,
            self.prompt,
            "--name",
            self.name,
            "--deliver",
            self.deliver,
            "--repeat",
            str(self.repeat),
            "--script",
            self.script,
        )

    def edit_args(self, job_id: str) -> tuple[str, ...]:
        return (
            "cron",
            "edit",
            job_id,
            "--schedule",
            self.schedule,
            "--prompt",
            self.prompt,
            "--name",
            self.name,
            "--deliver",
            self.deliver,
            "--repeat",
            str(self.repeat),
            "--script",
            self.script,
        )


Runner = Callable[..., Any]
ManagedCronFactory = Callable[[Path, Callable[[tuple[str, ...]], Any]], ManagedCron]



@dataclass(frozen=True)
class _CanonicalRuntime:
    configuration: Mapping[str, Any]
    private_env: Mapping[str, str]

def _canonical_delivery_armed(state: _CanonicalRuntime) -> bool:
    checkin_state = state.configuration.get("checkin_state")
    recurring = checkin_state.get("recurring") if isinstance(checkin_state, Mapping) else None
    return bool(
        state.configuration.get("delivery_capability_enabled") is True
        and state.configuration.get("delivery_execution_armed") is True
        and isinstance(recurring, Mapping)
        and recurring.get("status") == "armed"
    )


def _first_message_requested(state: _CanonicalRuntime) -> bool:
    checkin_state = state.configuration.get("checkin_state")
    first_message = (
        checkin_state.get("first_message") if isinstance(checkin_state, Mapping) else None
    )
    return bool(
        state.configuration.get("first_message") == "gumi_sends_first"
        and isinstance(first_message, Mapping)
        and first_message.get("status") == "requested"
    )


def recurring_delivery_armed(
    *, relic_home: Path | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """Return whether canonical User artifacts permit recurring delivery."""
    state = _load_canonical_state(relic_home=relic_home, environ=environ)
    return bool(state and _canonical_delivery_armed(state) and _telegram_delivery(state))


def first_message_requested(
    *,
    relic_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> bool:
    """Return whether canonical User artifacts request a first Telegram message."""
    state = _load_canonical_state(
        relic_home=relic_home, environ=environ, hermes_home=hermes_home
    )
    return bool(state and _first_message_requested(state) and _telegram_delivery(state))


def media_delivery_armed(
    media_type: str,
    *,
    relic_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> bool | None:
    """Return media readiness, or ``None`` when no canonical User exists."""
    environment = os.environ if environ is None else environ
    try:
        state = _read_canonical_state(
            _relic_home(relic_home, environment),
            _hermes_profile_home(environment, hermes_home),
        )
    except (OSError, ValueError):
        return False
    if state is None:
        return None
    if not _canonical_delivery_armed(state) or _telegram_delivery(state) is None:
        return False
    if media_type == "text":
        return True
    media_name = {"voice": "audio", "image": "images", "music": "music"}.get(media_type)
    media = state.configuration.get("media")
    setting = media.get(media_name) if isinstance(media, Mapping) and media_name else None
    return bool(
        isinstance(setting, Mapping)
        and setting.get("selected") is True
        and setting.get("enabled") is True
    )


class RuntimeActivator:
    """Reconcile ManagedCron and the first-message one-shot from User artifacts."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        environ: Mapping[str, str] | None = None,
        relic_home: Path | None = None,
        hermes_base_home: Path | None = None,
        managed_cron_factory: ManagedCronFactory | None = None,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.relic_home = _relic_home(relic_home, self.environ)
        self.hermes_base_home = (
            Path(hermes_base_home).expanduser() if hermes_base_home is not None else None
        )
        if self.hermes_base_home is not None:
            hermes_profile_home = self.hermes_base_home / "profiles" / PROFILE_NAME
            self.environ["HERMES_HOME"] = str(hermes_profile_home)
            self.environ["HERMES_PROFILES_HOME"] = str(self.hermes_base_home / "profiles")
        else:
            hermes_profile_home = Path(
                self.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
            )
        self.hermes_profile_home = hermes_profile_home
        self.jobs_path = (
            hermes_profile_home / "cron" / "jobs.json"
            if self.hermes_base_home is not None
            else _hermes_profile_home(self.environ) / "cron" / "jobs.json"
        )
        self.runner = runner or _run
        hermes_scripts_dir = hermes_profile_home / "scripts"
        self.managed_cron_factory = managed_cron_factory or (
            lambda base_home, cron_runner: ManagedCron(
                base_home=base_home,
                runner=cron_runner,
                scripts_dir=hermes_scripts_dir,
            )
        )

    def reconcile(self) -> RuntimeActivation:
        state = _required_canonical_state(
            self.relic_home,
            self.environ,
            hermes_base_home=self.hermes_base_home,
        )
        telegram = _telegram_delivery(state)
        delivery_armed = _canonical_delivery_armed(state) and telegram is not None
        first_message = (
            _FirstMessageSpec(
                name=FIRST_MESSAGE_JOB_NAME,
                schedule=_FIRST_MESSAGE_SCHEDULE,
                prompt=_FIRST_MESSAGE_PROMPT,
                deliver=f"telegram:{telegram}",
                repeat=1,
                script=FIRST_MESSAGE_SCRIPT_NAME,
            )
            if telegram is not None and _first_message_requested(state)
            else None
        )
        changes = _FirstMessageChanges(self)
        try:
            manager = self.managed_cron_factory(self.relic_home, self._managed_cron_runner)
            if isinstance(manager, ManagedCron):
                manager.configure_hermes_jobs_path(self.hermes_profile_home)
            manager.reconcile(delivery_armed=delivery_armed)
            self._reconcile_first_message(first_message, changes)
        except Exception as exc:
            changes.rollback()
            if isinstance(exc, RuntimeActivationError):
                # RuntimeActivationError messages are fixed, secret-free strings.
                raise RuntimeActivationError(f"cron activation failed: {exc}") from None
            raise RuntimeActivationError("cron activation failed") from exc
        return RuntimeActivation(
            recurring_active=delivery_armed,
            first_message_scheduled=first_message is not None,
        )

    def _managed_cron_runner(self, command: tuple[str, ...]) -> CommandResult:
        return self._execute(*command)

    def _reconcile_first_message(
        self, spec: _FirstMessageSpec | None, changes: "_FirstMessageChanges"
    ) -> None:
        owned = [
            job for job in self._list_jobs() if job.get("name") == FIRST_MESSAGE_JOB_NAME
        ]
        if spec is None:
            for job in owned:
                self._remove(job)
                changes.removed.append(job)
            self._sync_script(changes, wanted=False)
            return
        self._sync_script(changes, wanted=True)
        if owned:
            job = owned[0]
            if _job_is_paused(job):
                self._remove(job)
                changes.removed.append(job)
                self._create(spec, changes)
            elif _first_message_spec(job) != spec:
                changes.edited.append((job, _first_message_spec(job)))
                self._edit(job, spec)
            for duplicate in owned[1:]:
                self._remove(duplicate)
                changes.removed.append(duplicate)
            return
        self._create(spec, changes)

    @property
    def first_message_script_path(self) -> Path:
        return self.hermes_profile_home / "scripts" / FIRST_MESSAGE_SCRIPT_NAME

    def _sync_script(self, changes: "_FirstMessageChanges", *, wanted: bool) -> None:
        """Write (idempotently) or remove the profile script the job runs."""
        path = self.first_message_script_path
        try:
            current = path.read_text(encoding="utf-8") if path.is_file() else None
        except (OSError, UnicodeError):
            current = None
        if wanted:
            content = _first_message_script()
            if current == content:
                return
            changes.remember_script(path, current)
            write_private_script(path, content)
        elif path.is_symlink() or path.exists():
            changes.remember_script(path, current)
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _command(self, *args: str) -> tuple[str, ...]:
        return (sys.executable, "-m", "hermes_cli.main", *args)

    def _execute(self, *args: str) -> CommandResult:
        try:
            result = _command_result(self.runner(self._command(*args), env=self.environ))
        except Exception as exc:
            raise RuntimeActivationError("cron command failed") from exc
        if result.returncode != 0:
            raise RuntimeActivationError("cron command failed")
        return result

    def _list_jobs(self) -> list[dict[str, Any]]:
        # Stock Hermes has no JSON listing: read the profile's cron/jobs.json.
        return _read_jobs_file(self.jobs_path)

    def _create(self, spec: _FirstMessageSpec, changes: "_FirstMessageChanges") -> None:
        job_id = _created_job_id(self._execute(*spec.create_args()).stdout)
        if job_id is None:
            named = [j for j in self._list_jobs() if j.get("name") == spec.name]
            job_id = _job_id(named[0]) if len(named) == 1 else None
        if job_id is None:
            raise RuntimeActivationError("cron create returned no job id")
        changes.created.append(job_id)

    def _edit(self, job: Mapping[str, Any], spec: _FirstMessageSpec) -> None:
        job_id = _job_id(job)
        if job_id is None:
            raise RuntimeActivationError("owned cron job is missing an id")
        self._execute(*spec.edit_args(job_id))

    def _remove(self, job: Mapping[str, Any]) -> None:
        job_id = _job_id(job)
        if job_id is None:
            raise RuntimeActivationError("owned cron job is missing an id")
        self._execute("cron", "remove", job_id)


@dataclass
class _FirstMessageChanges:
    activator: RuntimeActivator
    created: list[str]
    edited: list[tuple[Mapping[str, Any], _FirstMessageSpec | None]]
    removed: list[Mapping[str, Any]]
    script: tuple[Path, str | None] | None

    def __init__(self, activator: RuntimeActivator) -> None:
        self.activator = activator
        self.created = []
        self.edited = []
        self.removed = []
        self.script = None

    def remember_script(self, path: Path, previous: str | None) -> None:
        if self.script is None:  # keep the state from before the first change
            self.script = (path, previous)

    def _restore_script(self) -> None:
        if self.script is None:
            return
        path, previous = self.script
        try:
            if previous is None:
                path.unlink()
            else:
                write_private_script(path, previous)
        except Exception:
            pass

    def rollback(self) -> None:
        self._restore_script()  # before jobs: re-creating a job needs its script
        for job_id in reversed(self.created):
            _ignore_failure(self.activator._execute, "cron", "remove", job_id)
        for job, spec in reversed(self.edited):
            job_id = _job_id(job)
            if job_id is not None and spec is not None:
                _ignore_failure(self.activator._execute, *spec.edit_args(job_id))
        for job in reversed(self.removed):
            spec = _first_message_spec(job)
            if spec is not None:
                _ignore_failure(self.activator._execute, *spec.create_args())


def activate_runtime(
    *,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
    relic_home: Path | None = None,
    hermes_base_home: Path | None = None,
) -> RuntimeActivation:
    """Activate or revoke owned jobs using canonical User artifacts."""
    return RuntimeActivator(
        runner=runner,
        environ=environ,
        relic_home=relic_home,
        hermes_base_home=hermes_base_home,
    ).reconcile()


def gate(
    mode: str = "recurring",
    *,
    environ: Mapping[str, str] | None = None,
    relic_home: Path | None = None,
    out: TextIO | None = None,
) -> bool:
    """Print the narrow pre-run decision without exposing private values."""
    allowed = (
        first_message_requested(relic_home=relic_home, environ=environ)
        if mode == "first-message"
        else recurring_delivery_armed(relic_home=relic_home, environ=environ)
    )
    print("ARMED" if allowed else "BLOCKED", file=out or sys.stdout)
    return allowed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relic.user_runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    gate_parser = commands.add_parser("gate")
    gate_parser.add_argument(
        "mode", nargs="?", choices=("recurring", "first-message"), default="recurring"
    )
    args = parser.parse_args(argv)
    if args.command == "gate":
        gate(args.mode)
        return 0
    return 2


def _load_canonical_state(
    *,
    relic_home: Path | None,
    environ: Mapping[str, str] | None,
    hermes_home: Path | None = None,
) -> _CanonicalRuntime | None:
    environment = os.environ if environ is None else environ
    try:
        return _read_canonical_state(
            _relic_home(relic_home, environment),
            _hermes_profile_home(environment, hermes_home),
        )
    except (OSError, ValueError):
        return None


def _required_canonical_state(
    relic_home: Path,
    environ: Mapping[str, str],
    *,
    hermes_base_home: Path | None = None,
) -> _CanonicalRuntime:
    try:
        profile_home = (
            Path(hermes_base_home) / "profiles" / PROFILE_NAME
            if hermes_base_home is not None
            else _hermes_profile_home(environ)
        )
        state = _read_canonical_state(relic_home, profile_home)
    except (OSError, ValueError) as exc:
        raise RuntimeActivationError("canonical User artifacts are unavailable") from exc
    if state is None:
        raise RuntimeActivationError("canonical User artifacts are unavailable")
    return state


def _read_canonical_state(
    relic_home: Path, hermes_profile_home: Path
) -> _CanonicalRuntime | None:
    registry = ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=hermes_profile_home.parent,
    )
    configuration = registry.load_user_configuration()
    if configuration is None:
        return None
    # The host must supply the target profile's Hermes credential context;
    # constructing this registry does not switch or verify a secret scope.
    return _CanonicalRuntime(
        configuration=configuration,
        private_env=registry.load_user_private_env(),
    )


def _hermes_profile_home(
    environ: Mapping[str, str], hermes_home: Path | None = None
) -> Path:
    if hermes_home is not None:
        home = Path(hermes_home)
        if home.name == PROFILE_NAME:
            return home
        return home / "profiles" / PROFILE_NAME
    profiles_home = environ.get("HERMES_PROFILES_HOME", "").strip()
    if profiles_home:
        return Path(profiles_home) / PROFILE_NAME
    home = environ.get("HERMES_HOME", "").strip()
    if home:
        candidate = Path(home)
        if candidate.name == PROFILE_NAME:
            return candidate
        return candidate / "profiles" / PROFILE_NAME
    return Path.home() / ".hermes" / "profiles" / PROFILE_NAME




def _relic_home(relic_home: Path | None, environ: Mapping[str, str] | None) -> Path:
    if relic_home is not None:
        return Path(relic_home)
    value = (environ or os.environ).get("RELIC_HOME", "").strip()
    return Path(value) if value else Path.home() / ".relic"


def _telegram_delivery(state: _CanonicalRuntime) -> str | None:
    references = state.configuration.get("credential_references")
    if not isinstance(references, Mapping):
        return None
    token = state.private_env.get("TELEGRAM_BOT_TOKEN")
    target = state.private_env.get("TELEGRAM_HOME_CHANNEL")
    if not isinstance(token, str) or not isinstance(target, str):
        return None
    if not _TELEGRAM_TOKEN.fullmatch(token.strip()) or not _TELEGRAM_TARGET.fullmatch(target.strip()):
        return None
    if not (
        _credential_reference_matches(references, "TELEGRAM_BOT_TOKEN", token)
        and _credential_reference_matches(references, "TELEGRAM_HOME_CHANNEL", target)
    ):
        return None
    return target.strip()


def _credential_reference_matches(
    references: Mapping[str, Any], name: str, value: str
) -> bool:
    reference = references.get(name)
    if (
        not isinstance(reference, Mapping)
        or set(reference) != {"location", "fingerprint"}
        or reference.get("location") != _PRIVATE_HERMES_ENV_LOCATION
    ):
        return False
    fingerprint = reference.get("fingerprint")
    return isinstance(fingerprint, str) and fingerprint == hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def _read_jobs_file(path: Path) -> list[dict[str, Any]]:
    """Read a profile's ``cron/jobs.json``; refuses symlinks and non-files."""
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise RuntimeActivationError("Hermes cron jobs file is unavailable")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeActivationError("Cannot read Hermes cron jobs file") from exc
    return _jobs_from_json(text) if text.strip() else []


def is_first_message_job(hermes_home: Path, job_id: str) -> bool:
    """Whether ``job_id`` is EXACTLY the id of the owned first-message job.

    False when the jobs file is missing, unreadable, a symlink or malformed.
    """
    if not job_id:
        return False
    try:
        jobs = _read_jobs_file(Path(hermes_home) / "cron" / "jobs.json")
    except RuntimeActivationError:
        return False
    return any(
        job.get("name") == FIRST_MESSAGE_JOB_NAME and _job_id(job) == job_id for job in jobs
    )


def mark_first_message_sent(
    *,
    relic_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> bool:
    """Record that the first message was delivered (``requested`` -> ``sent``)."""
    environment = os.environ if environ is None else environ
    profile_home = _hermes_profile_home(environment, hermes_home)
    registry = ProfileRegistry(
        relic_home=_relic_home(relic_home, environment),
        hermes_profiles_home=profile_home.parent,
    )
    return registry.mark_first_message_sent()


def _jobs_from_json(raw: str) -> list[dict[str, Any]]:
    data = _json_result(raw)
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = next((data[key] for key in ("jobs", "data", "items") if key in data), None)
    else:
        jobs = None
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise RuntimeActivationError("cron list returned invalid JSON")
    return [dict(job) for job in jobs]


def _json_result(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeActivationError("cron command returned invalid JSON") from exc


def _command_result(value: Any) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        return CommandResult(*value)
    return CommandResult(
        returncode=int(value.returncode),
        stdout=str(value.stdout or ""),
        stderr=str(value.stderr or ""),
    )


def _run(command: Sequence[str], *, env: Mapping[str, str]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        env=dict(env),
        timeout=60,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _job_id(job: Any) -> str | None:
    if not isinstance(job, Mapping):
        return None
    value = job.get("id") or job.get("job_id")
    return value if isinstance(value, str) and value else None


def _first_message_script() -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 0 ]]; then
    printf '%s\\n' 'CANONICAL_ACTION_ERROR: generated action accepts no arguments' >&2
    exit 64
fi

export RELIC_HOME="${{RELIC_HOME:-$HOME/.relic}}"
exec "${{RELIC_PYTHON:-{sys.executable}}}" -m relic.user_runtime gate first-message
"""


def _first_message_spec(job: Mapping[str, Any]) -> _FirstMessageSpec | None:
    name = job.get("name")
    schedule = job.get("schedule")
    prompt = job.get("prompt")
    deliver = job.get("deliver", job.get("delivery"))
    repeat = job.get("repeat")
    script = job.get("script")
    if isinstance(schedule, Mapping):
        # Stock Hermes stores the PARSED schedule (a dict with kind/run_at/display),
        # from which the original "1m" is not reconstructable. Treat it as the
        # desired one-shot schedule: comparing it would make every reconcile
        # ``cron edit --schedule`` and push the first message further away.
        schedule = _FIRST_MESSAGE_SCHEDULE
    if isinstance(repeat, Mapping):
        repeat = repeat.get("times")  # stock Hermes: {"times": N, "completed": M}
    if name != FIRST_MESSAGE_JOB_NAME or not all(
        isinstance(value, str) for value in (schedule, prompt, deliver, script)
    ):
        return None
    if isinstance(repeat, bool):
        return None
    try:
        repeat = int(repeat)
    except (TypeError, ValueError):
        return None
    return _FirstMessageSpec(name, schedule, prompt, deliver, repeat, script)


def _job_is_paused(job: Mapping[str, Any]) -> bool:
    return job.get("enabled") is False or job.get("paused") is True or job.get("status") == "paused"


def _ignore_failure(action: Callable[..., Any], *args: str) -> None:
    try:
        action(*args)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
