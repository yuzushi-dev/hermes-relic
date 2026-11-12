"""Standalone operator commands cannot authenticate the target Hermes scope."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("command", ["install", "configure"])
def test_cli_never_resolves_profile_a_credentials_for_target_b(tmp_path, command):
    profile = tmp_path / "profiles" / "relic-gumi"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("TELEGRAM_BOT_TOKEN=test-profile-b\n")
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from agent import secret_scope
from relic import gumi_install as cli
profile = Path(sys.argv[1])
calls = []
def forbidden(*args, **kwargs):
    calls.append(True)
    raise AssertionError("standalone CLI must not resolve credentials")
secret_scope.get_secret = forbidden
class Installer:
    def install(self, **kwargs):
        return SimpleNamespace(profile_home=profile)
cli.Installer = Installer
result = cli.main([sys.argv[2], "--hermes-home", str(profile.parent.parent)])
assert result == (0 if sys.argv[2] == "install" else 1)
assert calls == []
assert list(profile.iterdir()) == [profile / ".env"]
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(profile), command],
        env={"PATH": os.defpath, "HOME": str(tmp_path),
             "PYTHONPATH": os.pathsep.join(sys.path),
             "TELEGRAM_BOT_TOKEN": "test-profile-a"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout + result.stderr
    assert "Credentials not checked" in text
    assert "test-profile-a" not in text and "test-profile-b" not in text
    assert "Missing in" not in text
    assert "/gumi-setup" in text
    assert "Settings -> Plugins" in text
