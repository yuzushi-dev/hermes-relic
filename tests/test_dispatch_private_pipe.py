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


def test_real_child_inherits_network_settings_but_not_provider_credentials(
    monkeypatch, tmp_path, scoped_secrets,
):
    network_settings = {
        "SSL_CERT_FILE": "/test-ca/cert.pem",
        "SSL_CERT_DIR": "/test-ca/certs",
        "REQUESTS_CA_BUNDLE": "/test-ca/requests.pem",
        "CURL_CA_BUNDLE": "/test-ca/curl.pem",
        "HTTP_PROXY": "http://test-user:test-password@proxy.invalid:8080",
        "HTTPS_PROXY": "http://secure-proxy.invalid:8080",
        "ALL_PROXY": "socks5://all-proxy.invalid:1080",
        "NO_PROXY": "localhost,test.invalid",
        "http_proxy": "http://lower-proxy.invalid:8080",
        "https_proxy": "http://lower-secure-proxy.invalid:8080",
        "all_proxy": "socks5://lower-all-proxy.invalid:1080",
        "no_proxy": "127.0.0.1,lower-test.invalid",
    }
    provider_credentials = {
        "OPENAI_API_KEY": "test-openai-key",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
    }
    for name, value in {**network_settings, **provider_credentials}.items():
        monkeypatch.setenv(name, value)
    receipt = tmp_path / "network-receipt.json"
    script = '''
import json, os, sys
from pathlib import Path
json.load(sys.stdin)
names = json.loads(sys.argv[2])
Path(sys.argv[1]).write_text(json.dumps({name: os.environ.get(name) for name in names}))
'''
    real_spawn = subprocess.Popen
    children = []

    def spawn(command, **kwargs):
        child = real_spawn([
            sys.executable, "-c", script, str(receipt),
            json.dumps([*network_settings, *provider_credentials]),
        ], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        assert plugin._spawn_dispatch(
            "test-message", "checkin", tmp_path, str(tmp_path / "relic"), "test-scope",
        ) is True
        assert children[0].wait(timeout=10) == 0
        assert json.loads(receipt.read_text()) == {
            **network_settings, **dict.fromkeys(provider_credentials),
        }
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_successful_real_handoff_survives_receipt_log_failure(
    monkeypatch, tmp_path, scoped_secrets,
):
    # A directory at the log file path deterministically makes open("a") fail.
    (tmp_path / "logs" / "cron-dispatch.log").mkdir(parents=True)
    receipt = tmp_path / "worker-receipt.json"
    script = '''
import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
Path(sys.argv[1]).write_text(json.dumps(payload["job"]["message_text"]))
'''
    real_spawn = subprocess.Popen
    children = []

    def spawn(command, **kwargs):
        child = real_spawn([sys.executable, "-c", script, str(receipt)], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        assert plugin._spawn_dispatch(
            "test-successful-handoff", "checkin", tmp_path,
            str(tmp_path / "relic"), "test-scope",
        ) is True
        assert children[0].wait(timeout=10) == 0
        assert json.loads(receipt.read_text()) == "test-successful-handoff"
    finally:
        for child in children:
            try:
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


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


@pytest.mark.parametrize("failure", ["oversize", "nonreading"])
def test_failed_handoff_clears_pending_checkin(monkeypatch, tmp_path, scoped_secrets, failure):
    import sqlite3
    from datetime import datetime, timezone
    from tests.test_hermes_directory_plugin import (
        _write_cron_reservation, _bridge_module, _dependencies, _registered_hooks,
    )

    hermes_home, relic_home = _write_cron_reservation(
        tmp_path, kind="checkin", job_name="gumi_checkin_message", job_id="handofffail",
    )
    db_path = relic_home / "user/checkin/relic.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.executescript('''
            CREATE TABLE checkin_exchanges (id INTEGER PRIMARY KEY, facet_id TEXT,
                question_text TEXT, reply_text TEXT, asked_at TEXT);
            CREATE TABLE checkin_features (id INTEGER PRIMARY KEY, tick_id TEXT,
                posture TEXT, created_at TEXT);
        ''')
        now = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO checkin_exchanges VALUES (1, 'facet', 'question', NULL, ?)", (now,))
        db.execute("INSERT INTO checkin_features VALUES (1, ?, 'ask', ?)", (now, now))
    with _bridge_module() as bridge:
        dependencies, *_ = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))
    monkeypatch.setattr(plugin, "_first_message_output", lambda *args: "not delivered")
    real_spawn = subprocess.Popen
    children = []
    def spawn(command, **kwargs):
        if failure == "oversize":
            pytest.fail("oversized payload spawned a child")
        child = real_spawn([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        result = hooks["transform_llm_output"](
            response_text="x" * (300_000 if failure == "oversize" else 100_000),
            platform="cron", session_id="cron_handofffail_20260101_120000",
        )
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT count(*) FROM checkin_exchanges").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM checkin_features").fetchone() == (0,)
        assert result == "not delivered"
        assert all(child.poll() is not None for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


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
