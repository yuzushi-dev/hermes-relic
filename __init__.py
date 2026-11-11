"""Checkout directory-plugin bridge delegated to the packaged implementation."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


def _ensure_relic_importable() -> None:
    """Make the ``relic`` package importable from this plugin directory.

    The catalog layout puts the repository root in the plugin directory, so the
    package sits in ``<plugin dir>/src``. That bundled copy is the reviewed code at the
    pinned commit, so it takes precedence over any other ``relic`` on ``sys.path``.
    A ``relic`` already imported in this process is left alone: it cannot be swapped.
    """
    source = Path(__file__).resolve().parent / "src"
    if not (source / "relic").is_dir() or "relic" in sys.modules:
        return
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    importlib.invalidate_caches()


_ensure_relic_importable()

from relic.hermes_plugin import directory_plugin as _bridge  # noqa: E402


PLUGIN_KEY = _bridge.PLUGIN_KEY
SAFE_OUTPUT_REPLACEMENT = _bridge.SAFE_OUTPUT_REPLACEMENT
_RelicDependencies = _bridge._RelicDependencies
_load_relic_dependencies = _bridge._load_relic_dependencies
_text = _bridge._text
_load_user_runtime = _bridge._load_user_runtime
_foreground_user_turn = _bridge._foreground_user_turn
_runtime_scope_env = _bridge._runtime_scope_env
_runtime_memory_provider = _bridge._runtime_memory_provider
_context_text = _bridge._context_text
_require_host_api = _bridge._require_host_api
_register_hooks = _bridge._register_hooks


def register(ctx: Any) -> None:
    _bridge.register(
        ctx,
        dependencies_loader=_load_relic_dependencies,
        callback_module=__name__,
    )
