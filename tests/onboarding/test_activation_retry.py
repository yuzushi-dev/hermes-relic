"""Remedies for a failed activation: ``/gumi-setup retry`` and CLI cleanup."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from onboarding.test_telegram_hook import Env


def _complete_with_error(env: Env) -> None:
    asyncio.run(env.run_quick())
    progress = env.progress()
    progress["activation_error"] = "RuntimeActivationError"
    env.controller._store().save(progress)


def test_setup_retry_reruns_only_activation_and_clears_the_error(tmp_path):
    env = Env(tmp_path)
    _complete_with_error(env)
    answers = env.progress()["answers"]
    runs = len(env.activations)

    reply = env.controller.setup_command("retry")

    assert reply == "All set. You can talk to me now."
    assert len(env.activations) == runs + 1
    progress = env.progress()
    assert "activation_error" not in progress
    assert progress["status"] == "complete" and progress["answers"] == answers


def test_setup_retry_failure_keeps_the_error(tmp_path, caplog):
    env = Env(tmp_path)
    _complete_with_error(env)

    def boom():
        raise RuntimeError("token-in-message")

    env.controller.activate = boom
    with caplog.at_level(logging.WARNING):
        reply = env.controller.setup_command("retry")

    assert reply == "Scheduling failed again. Run `relic-gumi verify`."
    assert env.progress()["activation_error"] == "RuntimeActivationError"
    assert "RuntimeError" in caplog.text and "token-in-message" not in caplog.text


def test_setup_retry_without_error_or_completion_does_nothing(tmp_path):
    env = Env(tmp_path)
    assert env.controller.setup_command("retry") == "Send /start to begin"
    asyncio.run(env.run_quick())
    runs = len(env.activations)
    assert env.controller.setup_command("retry") == "Nothing to retry."
    assert len(env.activations) == runs
    assert env.progress()["status"] == "complete"


def test_configure_success_clears_a_stale_activation_error(tmp_path, monkeypatch):
    import relic.gumi_install as cli
    from relic.onboarding.store import ProgressStore

    relic_home = tmp_path / "relic"
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    store = ProgressStore(relic_home / "user")
    store.save({"status": "complete", "answers": {}, "activation_error": "RuntimeActivationError"})

    class FakeOnboarding:
        def __init__(self, registry):
            pass

        def run(self):
            return object()

    monkeypatch.setattr(cli, "UserOnboarding", FakeOnboarding)
    monkeypatch.setattr(
        cli,
        "activate_runtime",
        lambda **kw: SimpleNamespace(recurring_active=True, first_message_scheduled=False),
    )

    assert cli.main(["configure", "--hermes-home", str(tmp_path)]) == 0
    saved = store.load()
    assert "activation_error" not in saved and saved["status"] == "complete"
