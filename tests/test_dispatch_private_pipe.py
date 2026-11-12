"""Actual OS-pipe/subprocess privacy and backpressure regressions."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from relic.hermes_plugin import directory_plugin as plugin
from relic.secrets import SECRET_NAMES


def test_job_and_credentials_cross_a_real_private_pipe(monkeypatch, tmp_path, scoped_secrets):
    scoped_secrets["TELEGRAM_BOT_TOKEN"] = "test-private-token"
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, "test-wrong-profile")
    marker = tmp_path / "receipt.json"
    script = '''
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
Path(sys.argv[1]).write_text(json.dumps({
    "job_received": payload.get("job", {}).get("message_text") == "test-private-conversation" + "文" * 20_000,
    "scope_received": payload.get("credentials", {}).get("TELEGRAM_BOT_TOKEN") == "test-private-token",
}))
'''
    real_spawn = subprocess.Popen
    captured = {}
    def spawn(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        process = real_spawn([sys.executable, "-c", script, str(marker)], **kwargs)
        captured["process"] = process
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    plugin._spawn_dispatch("test-private-conversation" + "文" * 20_000, "checkin", tmp_path, str(tmp_path / "relic"), "test-scope")
    assert captured["process"].wait(timeout=10) == 0
    assert "test-private-conversation" not in str(captured["command"])
    assert "test-private-token" not in str(captured["command"])
    assert not (set(SECRET_NAMES) & captured["env"].keys())
    assert json.loads(marker.read_text()) == {"job_received": True, "scope_received": True}
    log = (tmp_path / "logs/cron-dispatch.log").read_text()
    assert "test-private-conversation" not in log and "test-private-token" not in log


def test_nonreading_child_does_not_deadlock_caller(monkeypatch, tmp_path, scoped_secrets):
    real_spawn = subprocess.Popen
    children = []
    def spawn(command, **kwargs):
        process = real_spawn([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    started = time.monotonic()
    try:
        plugin._spawn_dispatch("x" * 100_000, "checkin", tmp_path, str(tmp_path / "relic"), "test-scope")
        assert time.monotonic() - started < 3
        assert children[0].poll() is not None
        assert children[0].stdin.closed
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_oversized_payload_is_rejected_before_spawn(monkeypatch, tmp_path, scoped_secrets):
    def forbidden(*args, **kwargs):
        pytest.fail("oversized payload must not spawn a worker")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    plugin._spawn_dispatch("x" * 300_000, "checkin", tmp_path, str(tmp_path / "relic"), "test-scope")


@pytest.mark.parametrize("payload", [b"{" , b"x" * (256 * 1024 + 1), b"[]", b'{"job":{},"credentials":{}}'])
def test_worker_rejects_invalid_or_oversized_private_input(tmp_path, payload):
    import os
    script = """
from relic.hermes_plugin import directory_plugin as plugin
def forbidden(*args, **kwargs):
    raise AssertionError('invalid envelope dispatched')
plugin._dispatch_scoped_job = forbidden
plugin._run_dispatch_worker()
print('rejected')
"""
    result = subprocess.run([sys.executable, "-c", script], input=payload,
                            env={"PATH": os.defpath, "HOME": str(tmp_path),
                                 "PYTHONPATH": os.pathsep.join(sys.path)},
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == b"rejected\n" and result.stderr == b""

