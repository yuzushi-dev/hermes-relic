"""Secret access fails closed instead of falling back to ambient credentials."""
import builtins
import sys
import types

import pytest


@pytest.mark.parametrize("name", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL", "GEMINI_API_KEY", "OLLAMA_API_KEYS"])
def test_secret_uses_active_hermes_scope(monkeypatch, name):
    from relic.secrets import secret_value

    calls = []
    module = types.ModuleType("agent.secret_scope")
    module.get_secret = lambda key: calls.append(key) or "test-token"
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    monkeypatch.setenv(name, "test-ambient-token")
    assert secret_value(name) == "test-token"
    assert calls == [name]


@pytest.mark.parametrize("failure", [ImportError, RuntimeError])
def test_secret_unavailable_fails_closed(monkeypatch, failure):
    from relic.secrets import secret_value

    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "agent.secret_scope":
            raise failure("test-secret-must-not-leak")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    monkeypatch.setenv("GEMINI_API_KEY", "test-ambient-token")
    assert secret_value("GEMINI_API_KEY") == ""


def test_scope_error_fails_closed(monkeypatch):
    from relic.secrets import secret_value

    module = types.ModuleType("agent.secret_scope")
    def denied(name):
        raise RuntimeError("test-secret-must-not-leak")
    module.get_secret = denied
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    assert secret_value("GEMINI_API_KEY") == ""
