"""Plumbing shared by the onboarding channels (Telegram, Desktop).

No question text lives here (see ``presentation.py`` and the catalog); this is
the runtime side: private-env lookup, completion check, activation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from relic.profile.registry import ProfileRegistry

logger = logging.getLogger(__name__)


def activation_detail(exc: Exception) -> str:
    """Sanitized cause: only RuntimeActivationError messages (fixed, secret-free)."""
    from relic.user_runtime import RuntimeActivationError

    return f" ({exc})" if isinstance(exc, RuntimeActivationError) else ""


def default_activate(
    relic_home: Path, hermes_base_home: Path
) -> Callable[[], Any]:
    def activate() -> Any:
        from relic.user_runtime import activate_runtime

        return activate_runtime(relic_home=relic_home, hermes_base_home=hermes_base_home)

    return activate


def env_value(
    env: Mapping[str, str],
    registry: ProfileRegistry,
    name: str,
    *,
    warn: Callable[[], None] | None = None,
) -> str:
    """Configuration value: the process env first, then the private profile .env."""
    value = (env.get(name) or "").strip()
    if value:
        return value
    try:
        return (registry._read_private_env().get(name) or "").strip()
    except ValueError:
        if warn is not None:
            warn()
        return ""
    except OSError:
        return ""


def is_complete(registry: ProfileRegistry, progress: Mapping[str, Any]) -> bool:
    if progress.get("status") != "complete":
        return False
    try:
        return all(p.is_file() for p in registry._artifact_paths().values())
    except (OSError, ValueError):
        return False
