"""resolve_hermes_home must honour Hermes' per-context home override.

Inside the gateway, ``HERMES_HOME`` is the multiplex base while the active
profile home is only exposed through ``hermes_constants.get_hermes_home_override``.
"""

from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import sys
from pathlib import Path
from types import ModuleType

import pytest

from relic.hermes_plugin.pending_reply import resolve_hermes_home


def _fake_constants(monkeypatch: pytest.MonkeyPatch, override) -> None:
    module = ModuleType("hermes_constants")
    module.get_hermes_home_override = lambda: override
    monkeypatch.setitem(sys.modules, "hermes_constants", module)


def test_override_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "base"))
    _fake_constants(monkeypatch, str(tmp_path / "base" / "profiles" / HERMES_USER_PROFILE_NAME))
    assert resolve_hermes_home() == tmp_path / "base" / "profiles" / HERMES_USER_PROFILE_NAME


def test_no_override_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_constants(monkeypatch, None)
    assert resolve_hermes_home() == Path(tmp_path)


def test_empty_override_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_constants(monkeypatch, "  ")
    assert resolve_hermes_home() == Path(tmp_path)


def test_empty_env_and_no_override_is_none(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "")
    _fake_constants(monkeypatch, None)
    assert resolve_hermes_home() is None


def test_missing_module_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_constants", None)  # forces ImportError
    assert resolve_hermes_home() == Path(tmp_path)


def test_module_exception_does_not_propagate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    module = ModuleType("hermes_constants")

    def boom():
        raise RuntimeError("boom")

    module.get_hermes_home_override = boom
    monkeypatch.setitem(sys.modules, "hermes_constants", module)
    assert resolve_hermes_home() == Path(tmp_path)
