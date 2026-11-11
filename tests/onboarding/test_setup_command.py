"""``/gumi-setup`` command, relic_home plumbing and private-env warning."""

from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import asyncio
import logging
from types import SimpleNamespace

from onboarding.test_telegram_hook import TOKEN, USER, Env, event
from relic.hermes_plugin import telegram_onboarding as tg


def test_setup_command_before_completion_asks_for_start(tmp_path):
    env = Env(tmp_path)
    assert env.controller.setup_command("") == "Send /start to begin"


def test_setup_command_restarts_completed_onboarding_keeping_ids(tmp_path):
    env = Env(tmp_path)
    asyncio.run(env.run_quick())
    assert env.progress()["status"] == "complete"
    before = env.progress()

    reply = env.controller.setup_command("")

    assert reply == "Onboarding restarted. Send any message to begin."
    after = env.progress()
    assert after["status"] == "in_progress"
    assert after["answers"] == {}
    assert after["chat_id"] == before["chat_id"]
    assert after["user_id"] == before["user_id"]
    assert env.controller._is_complete(after) is False
    # credentials and the previous config stay until the next save
    assert all(p.is_file() for p in env.controller._registry()._artifact_paths().values())
    # the next message starts the conversation again
    result = asyncio.run(env.controller.handle_message(event("hi")))
    assert result["action"] == "skip"
    texts = [str(c.get("text", "")) for c in env.bot.sent]
    assert not any("Please use the buttons" in t for t in texts)
    assert env.current().id == "notice"


def test_setup_command_handler_may_be_awaited_or_called(tmp_path):
    env = Env(tmp_path)
    assert env.controller.setup_command("x") == "Send /start to begin"


def test_default_activate_uses_controller_relic_home(tmp_path, monkeypatch):
    import relic.user_runtime as ur

    seen = {}
    monkeypatch.setattr(ur, "activate_runtime", lambda **kw: seen.update(kw))
    controller = tg.OnboardingController(
        relic_home=tmp_path / "relic",
        profiles_home=tmp_path / "profiles",
        hermes_base_home=tmp_path / "hermes",
        env={},
    )
    controller.activate()
    assert seen == {
        "relic_home": tmp_path / "relic",
        "hermes_base_home": tmp_path / "hermes",
    }


def test_profile_env_is_ignored_and_missing_credentials_pass_through(tmp_path, caplog):
    env = Env(tmp_path, TELEGRAM_ALLOWED_USERS="", TELEGRAM_BOT_TOKEN="")
    env_file = env.profiles_home / HERMES_USER_PROFILE_NAME / ".env"
    env_file.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere.env"
    target.write_text(
        f"TELEGRAM_ALLOWED_USERS={USER}\nTELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8"
    )
    env_file.symlink_to(target)  # a linked .env is refused by the registry
    with caplog.at_level(logging.WARNING):
        first = asyncio.run(env.controller.handle_message(event("/start")))
        second = asyncio.run(env.controller.handle_message(event("/start")))
    assert first is None and second is None
    assert env.bot.sent == []
    messages = [r.getMessage() for r in caplog.records if "onboarding disabled" in r.getMessage()]
    assert messages == []  # No allowlisted user, and no credential-file inspection.
    assert TOKEN not in caplog.text
