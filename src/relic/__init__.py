"""relic - local-first knowledge retrieval assistant."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

_EXPORTS: dict[str, str] = {
    # Persistence
    "MemoryBlock": "relic.persistence",
    "MemoryPersistence": "relic.persistence",
    "PrivacyLevel": "relic.persistence",
    "PrivacyTrace": "relic.persistence",
    # Control - Pause
    "PauseController": "relic.control.pause",
    "PauseState": "relic.control.pause",
    "PauseRecord": "relic.control.pause",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'relic' has no attribute {name!r}")
    module = import_module(_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value
