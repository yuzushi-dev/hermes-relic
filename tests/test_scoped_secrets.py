"""Secret access delegates profile and single-profile semantics to Hermes."""
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
    registry.hermes_user_home.mkdir(parents=True)
    registry.hermes_user_env_path.write_text("GEMINI_API_KEY=test-file-token\n")
    registry.hermes_user_env_path.chmod(0o600)
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
    from relic.hermes_plugin.telegram_onboarding import OnboardingController
    from relic.hermes_plugin.desktop_onboarding import DesktopOnboardingController
    from relic.gumi_plugin.checkin_media_dispatcher import _load_env
    from relic.checkin.attribution_jury import _load_keys
    from relic.user_runtime import _read_canonical_state
    for controller_type in (OnboardingController, DesktopOnboardingController):
        controller = object.__new__(controller_type)
        controller.env = {"GEMINI_API_KEY": "test-wrong-profile"}
        controller.relic_home = registry.relic_home
        controller.profiles_home = registry.hermes_profiles_home
        assert controller._value("GEMINI_API_KEY") == "test-token"
    assert _load_env(tmp_path) == {}
    assert _load_keys() == []
    assert _read_canonical_state(registry.relic_home, registry.hermes_user_home).private_env == {"GEMINI_API_KEY": "test-token"}
    # Test-only read through the saved original, after exercising trapped runtime paths.
    with original_path_open(registry.hermes_user_env_path, encoding="utf-8") as stream:
        assert stream.read() == "GEMINI_API_KEY=test-file-token\n"
    output = StringIO()
    onboarding = UserOnboarding(registry, io_in=StringIO("y\nn\nn\n"), io_out=output, environ={"GEMINI_API_KEY": "test-wrong-profile"}, gemini_validator=lambda key: key == "test-token")
    media, credentials = onboarding._collect_media({})
    assert media["images"]["enabled"] is True
    scoped_secrets.clear()
    assert onboarding._collect_telegram({}, {}) == {"delivery_capability_enabled": False, "delivery_execution_armed": False}
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL"):
        assert f"hermes --profile relic-gumi config set {name}" in output.getvalue()


def test_manifest_declares_optional_scoped_secrets():
    from pathlib import Path
    import yaml
    from relic.secrets import SECRET_NAMES

    manifest = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())
    assert manifest.get("requires_env") == []  # Core continuity also works without Telegram/media.
    fields = manifest.get("config_schema", {})
    assert {field["env"] for field in fields.values() if field["type"] == "secret"} == set(SECRET_NAMES)
    assert all(field.get("required") is False for field in fields.values())


def test_detached_delivery_never_inherits_ambient_credentials(monkeypatch, tmp_path, scoped_secrets):
    import io
    import json
    from types import SimpleNamespace
    from relic.hermes_plugin import directory_plugin
    from relic.secrets import SECRET_NAMES

    scoped_secrets["TELEGRAM_BOT_TOKEN"] = "test-scoped-token"
    monkeypatch.setattr(directory_plugin.os, "environ", {name: "test-wrong-profile" for name in SECRET_NAMES})
    captured = {}
    pipe = io.StringIO()
    class Pipe:
        def write(self, value):
            return pipe.write(value)
        def close(self):
            pass
    def spawn(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(stdin=Pipe())
    monkeypatch.setattr("subprocess.Popen", spawn)
    directory_plugin._spawn_dispatch("test-message", "checkin", tmp_path, str(tmp_path / "relic"), "test-scope")
    assert not (set(SECRET_NAMES) & captured["env"].keys())
    assert "test-scoped-token" not in str(captured["command"])
    assert json.loads(pipe.getvalue()) == {"TELEGRAM_BOT_TOKEN": "test-scoped-token"}
    assert "test-scoped-token" not in (tmp_path / "logs/cron-dispatch.log").read_text()


def test_detached_worker_restores_scope_in_a_real_child(tmp_path):
    import os
    import subprocess
    import textwrap

    script = textwrap.dedent('''
        from pathlib import Path
        from agent.secret_scope import get_secret, current_secret_scope
        from relic.hermes_plugin import directory_plugin as plugin
        from relic.secrets import secret_value
        import os
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-wrong-profile"
        seen = []
        def deliver(**job):
            assert secret_value("TELEGRAM_BOT_TOKEN") == "test-scoped-token"
            assert get_secret("GEMINI_API_KEY") is None
            seen.append(True)
        plugin._dispatch_and_record = deliver
        plugin._dispatch_scoped_job({"hermes_home": Path("/test-profile")}, {"TELEGRAM_BOT_TOKEN": "test-scoped-token"})
        assert seen == [True]
        assert current_secret_scope() is None
        print("scoped child passed")
    ''')
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": os.pathsep.join(sys.path), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scoped child passed"


def test_operator_dryrun_does_not_copy_credential_files(tmp_path):
    from relic.profile.manual_cron import isolated_state

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".env").write_text("GEMINI_API_KEY=test-token\n")
    (profile / "auth.json").write_text('{"token": "test-token"}')
    (profile / "config.yaml").write_text("model: test-model\n")
    with isolated_state(tmp_path / "relic", profile) as (_, copied, _):
        assert not (copied / ".env").exists()
        assert not (copied / "auth.json").exists()
        assert (copied / "config.yaml").read_text() == "model: test-model\n"


def test_legacy_cli_rejects_credential_files_before_reading(tmp_path, monkeypatch):
    from pathlib import Path
    from relic.profile.migration import read_private_env, MigrationError

    def forbidden(*args, **kwargs):
        pytest.fail("credential file was read")
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(MigrationError, match="hermes --profile relic-gumi config set"):
        read_private_env(tmp_path / ".env")


def test_scope_error_fails_closed(monkeypatch):
    from relic.secrets import secret_value

    module = types.ModuleType("agent.secret_scope")
    def denied(name):
        raise RuntimeError("test-secret-must-not-leak")
    module.get_secret = denied
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    assert secret_value("GEMINI_API_KEY") == ""


def test_setup_instructions_are_executable_and_warn_about_cli_exposure():
    from relic.secrets import credential_setup_commands
    text = credential_setup_commands()
    assert "Hermes Desktop" in text and "Settings -> Plugins" in text
    assert "masked" in text
    assert "config set TELEGRAM_BOT_TOKEN <value>" in text
    assert "shell history" in text and "command line" in text
    assert "user must run" in text and "Agents must never" in text


def test_real_hermes_single_profile_without_scope_uses_process_environment(monkeypatch):
    scope = pytest.importorskip("agent.secret_scope")
    from relic.secrets import secret_value
    token = scope.set_secret_scope(None)
    multiplex = scope.set_multiplex_context(False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-single-profile")
    try:
        assert secret_value("GEMINI_API_KEY") == "test-single-profile"
    finally:
        scope.reset_secret_scope(token)
        scope.reset_multiplex_context(multiplex)
