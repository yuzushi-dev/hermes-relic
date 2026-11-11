"""Hermes directory-plugin entry point packaged with RELIC Gumi.

The installer copies this directory into the profile together with a private copy of
the ``relic`` package under ``_vendor/``, so the plugin does not depend on ``relic``
being installed in whichever Hermes environment generation is current.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


def _ensure_relic_importable() -> None:
    """Prefer the bundled copy of ``relic`` so hooks run the code this plugin shipped with.

    A ``relic`` already imported in this process is left alone: it cannot be swapped.
    """
    vendor = Path(__file__).resolve().parent / "_vendor"
    if not (vendor / "relic").is_dir() or "relic" in sys.modules:
        return
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    importlib.invalidate_caches()


_ensure_relic_importable()

from relic.hermes_plugin.directory_plugin import register as _register  # noqa: E402


def register(ctx: Any) -> None:
    _register(ctx, callback_module=__name__)
