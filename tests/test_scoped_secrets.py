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


def test_runtime_consumers_resolve_active_scope_each_call(monkeypatch, tmp_path):
    from relic.checkin import attribution_jury as jury
    from relic.gumi_plugin import checkin_media_dispatcher as dispatcher

    values = {"GEMINI_API_KEY": "test-google", "TELEGRAM_BOT_TOKEN": "test-token", "OLLAMA_API_KEYS": "test-one,test-two,test-one"}
    calls = []
    module = types.ModuleType("agent.secret_scope")
    module.get_secret = lambda name: calls.append(name) or values.get(name)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-wrong-profile")
    assert dispatcher._get_api_key() == "test-google"
    assert dispatcher._load_env(tmp_path) == {"TELEGRAM_BOT_TOKEN": "test-token"}
    assert jury._load_keys() == ["test-one", "test-two"]
    assert jury._next_key() in {"test-one", "test-two"}
    values["OLLAMA_API_KEYS"] = "test-other-profile"
    assert jury._next_key() == "test-other-profile"
    assert set(calls) == {"GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_ALLOWED_USERS", "OLLAMA_API_KEYS"}


def test_registry_and_onboarding_never_open_dotenv(monkeypatch, tmp_path, scoped_secrets):
    from io import StringIO
    from pathlib import Path
    from relic.profile.registry import ProfileRegistry
    from relic.profile.user_onboarding import UserOnboarding
    from relic.onboarding.channel import env_value

    scoped_secrets["GEMINI_API_KEY"] = "test-token"
    registry = ProfileRegistry(relic_home=tmp_path / "relic", hermes_profiles_home=tmp_path / "profiles")
    user = registry.initialize_user()
    original_read = Path.read_text
    original_open = builtins.open
    original_path_open = Path.open
    def safe_read(path, *args, **kwargs):
        assert path.name != ".env", "plugin read a credential file"
        return original_read(path, *args, **kwargs)
    def safe_open(path, *args, **kwargs):
        assert Path(path).name != ".env", "plugin opened a credential file"
        return original_open(path, *args, **kwargs)
    def safe_path_open(path, *args, **kwargs):
        assert path.name != ".env", "plugin opened a credential file"
        return original_path_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", safe_read)
    monkeypatch.setattr(Path, "open", safe_path_open)
    monkeypatch.setattr(builtins, "open", safe_open)
    assert registry.load_user_private_env() == {"GEMINI_API_KEY": "test-token"}
    assert env_value({"GEMINI_API_KEY": "test-wrong-profile"}, registry, "GEMINI_API_KEY") == "test-token"
    from relic.onboarding.assemble import assemble, finalize
    finalize(registry, user, assemble({}, telegram_token="", chat_id=""))
    assert not registry.hermes_user_env_path.exists()
    output = StringIO()
    onboarding = UserOnboarding(registry, io_in=StringIO("y\nn\nn\n"), io_out=output, environ={"GEMINI_API_KEY": "test-wrong-profile"}, gemini_validator=lambda key: key == "test-token")
    media, credentials = onboarding._collect_media({})
    assert media["images"]["enabled"] is True
    scoped_secrets.clear()
    assert onboarding._collect_telegram({}, {}) == {"delivery_capability_enabled": False, "delivery_execution_armed": False}
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL"):
        assert f"hermes --profile relic-gumi config set {name}" in output.getvalue()


def test_scope_error_fails_closed(monkeypatch):
    from relic.secrets import secret_value

    module = types.ModuleType("agent.secret_scope")
    def denied(name):
        raise RuntimeError("test-secret-must-not-leak")
    module.get_secret = denied
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    assert secret_value("GEMINI_API_KEY") == ""
