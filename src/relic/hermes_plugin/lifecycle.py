from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class LifecycleState(str, Enum):
    PREFLIGHT = "preflight"
    INSTALLED = "installed"
    ENABLED = "enabled"
    HOST_V2_LOADED = "host-v2-loaded"
    THREE_HOOKS_VERIFIED = "three-hooks-verified"
    GATEWAY_READY = "gateway-ready"
    FAILED = "failed"


_ORDER = (
    LifecycleState.PREFLIGHT,
    LifecycleState.INSTALLED,
    LifecycleState.ENABLED,
    LifecycleState.HOST_V2_LOADED,
    LifecycleState.THREE_HOOKS_VERIFIED,
    LifecycleState.GATEWAY_READY,
)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    _private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class LifecycleStore:
    def __init__(self, profile_home: Path):
        self.profile_home = profile_home.expanduser()
        self.path = self.profile_home / ".relic-gumi" / "lifecycle.json"

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Cannot read RELIC Gumi lifecycle state at {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            raise RuntimeError(f"Invalid RELIC Gumi lifecycle state at {self.path}")
        return data

    def current(self) -> LifecycleState | None:
        data = self.load()
        if data is None:
            return None
        try:
            return LifecycleState(data["state"])
        except ValueError as exc:
            raise RuntimeError(f"Invalid RELIC Gumi lifecycle state at {self.path}") from exc

    def begin(self) -> None:
        pid, ppid = os.getpid(), os.getppid()
        try:
            cmdline = Path(f"/proc/{ppid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmdline = "<unknown>"
        print(f"[lifecycle] begin() called: pid={pid} ppid={ppid} ppid_cmdline={cmdline!r}", file=sys.stderr)
        self._write(LifecycleState.PREFLIGHT)

    def claim(self) -> None:
        """Silently record that this profile belongs to the installer, if nothing is recorded yet."""
        if self.current() is None:
            self._write(LifecycleState.PREFLIGHT)

    def advance(self, state: LifecycleState) -> None:
        if state is LifecycleState.FAILED:
            raise ValueError("Use fail() to record a failed lifecycle")
        current = self.current()
        if state is LifecycleState.PREFLIGHT:
            if current not in (None, LifecycleState.PREFLIGHT):
                raise RuntimeError(f"Cannot restart lifecycle from {current.value}; call begin()")
        elif current is not _ORDER[_ORDER.index(state) - 1]:
            current_name = "no state" if current is None else current.value
            raise RuntimeError(f"Cannot advance lifecycle from {current_name} to {state.value}")
        self._write(state)

    def fail(self, reason: str) -> None:
        self._write(LifecycleState.FAILED, reason=reason)

    def ready(self) -> bool:
        return self.current() is LifecycleState.GATEWAY_READY

    def _write(self, state: LifecycleState, *, reason: str | None = None) -> None:
        data: dict[str, Any] = {
            "state": state.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if reason:
            data["reason"] = reason
        write_private_json(self.path, data)


def outbound_allowed(profile_home: Path) -> bool:
    try:
        return LifecycleStore(profile_home).ready()
    except RuntimeError:
        return False


def require_outbound_ready(profile_home: Path) -> None:
    if not outbound_allowed(profile_home):
        raise RuntimeError(
            "RELIC Gumi outbound delivery is blocked until 'relic-gumi install' reaches gateway-ready."
        )
