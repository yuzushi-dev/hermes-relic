"""Review fixes: ownership, save/text race, name validation, hook robustness."""

from __future__ import annotations

import asyncio
import logging

from onboarding.test_telegram_hook import (
    CHAT,
    TOKEN,
    USER,
    Env,
    FakeQuery,
    event,
)
from relic.hermes_plugin import telegram_onboarding as tg
from relic.onboarding.engine import Engine
from relic.onboarding.questions import build_questions

USER_B = "987654321"
CHAT_B = "987654321"


async def drive_to(env: Env, qid: str) -> None:
    """Start and answer (quick path) until ``qid`` is the current question."""
    from onboarding.test_telegram_hook import QUICK

    await env.start()
    await env.tap_value("notice", "ok")
    for _ in range(200):
        question = env.current()
        if question is None or question.id == qid:
            return
        if question.kind == "text":
            if question.id == "tz":
                await env.controller.handle_message(event("Europe/Rome"))
            else:
                await env.tap(f"rg:{question.id}:skip")
        elif question.id in QUICK:
            await env.tap_value(question.id, QUICK[question.id])
        else:
            await env.tap_value(question.id, question.options[0].value)
    raise AssertionError("did not reach the question")


# --- F1 ----------------------------------------------------------------------


def test_second_allowed_user_cannot_answer_for_the_first(tmp_path):
    env = Env(tmp_path, TELEGRAM_ALLOWED_USERS=f"{USER},{USER_B}")

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.tap("rg:name:skip")
        assert env.current().id == "tz"
        before = env.progress()
        sent = len(env.bot.sent)
        result = await env.controller.handle_message(
            event("Europe/Rome", user_id=USER_B, chat_id=CHAT_B)
        )
        return before, sent, result

    before, sent, result = asyncio.run(main())
    assert result is None
    assert env.progress() == before
    assert len(env.bot.sent) == sent


# --- F2 ----------------------------------------------------------------------


class SlowQuery(FakeQuery):
    async def answer(self, text=None, **kwargs):
        await asyncio.sleep(0.05)
        await super().answer(text, **kwargs)


def test_text_racing_the_save_does_not_reset_a_complete_onboarding(tmp_path):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "confirm")
        options = [o.value for o in env.current().options]
        query = SlowQuery(f"rg:confirm:{options.index('save')}")
        results = await asyncio.gather(
            env.controller.on_callback(type("U", (), {"callback_query": query})(), None),
            env.controller.handle_message(event("hello")),
        )
        return results[1]

    result = asyncio.run(main())
    progress = env.progress()
    assert progress["status"] == "complete"
    assert progress["answers"].get("confirm") == "save"
    assert len(progress["answers"]) > 10
    assert result is None
    assert env.activations == [1]


def test_all_answered_but_not_complete_is_not_wiped(tmp_path):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "confirm")
        # simulate a crash right after the confirm answer was stored
        progress = env.progress()
        progress["answers"]["confirm"] = "save"
        env.controller._store().save(progress)
        assert env.current() is None
        return await env.controller.handle_message(event("hello"))

    result = asyncio.run(main())
    progress = env.progress()
    assert result == {"action": "skip", "reason": "relic-onboarding"}
    assert len(progress["answers"]) > 10
    assert "confirm" not in progress["answers"]
    assert "Here is your setup" in str(env.bot.sent[-1]["text"])


# --- F3 ----------------------------------------------------------------------


def test_overlong_or_multiline_name_is_rejected(tmp_path):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "name")
        answers = dict(env.progress()["answers"])
        for bad in ("x" * 4000, "Ada\nLovelace", "A" + "‮" + "da", "x" * 61):
            await env.controller.handle_message(event(bad))
            assert env.progress()["answers"] == answers
            assert env.current().id == "name"
            assert "single-line name" in str(env.bot.sent[-2]["text"])
        await env.controller.handle_message(event("Ada"))

    asyncio.run(main())
    assert env.progress()["answers"]["name"] == "Ada"


def test_name_skip_still_valid(tmp_path):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "name")
        await env.controller.handle_message(event("__skip__"))

    asyncio.run(main())
    assert env.progress()["answers"]["name"] == "__skip__"


def test_present_failure_does_not_propagate_or_leak_text(tmp_path, caplog):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "name")

        async def boom(**kwargs):
            raise RuntimeError("secret-text-" + kwargs.get("text", ""))

        env.bot.send_message = boom
        with caplog.at_level(logging.WARNING):
            return await env.controller.handle_message(event("Ada"))

    result = asyncio.run(main())
    assert result == {"action": "skip", "reason": "relic-onboarding"}
    assert env.progress()["answers"]["name"] == "Ada"
    assert "RuntimeError" in caplog.text
    assert "secret-text" not in caplog.text


# --- F5 / F6 -----------------------------------------------------------------


def test_hook_exception_still_skips_when_destined_for_onboarding(tmp_path, caplog):
    env = Env(tmp_path)

    def boom(*a, **k):
        raise OSError("disk")

    async def main():
        await env.start()
        env.controller._advance = boom
        with caplog.at_level(logging.WARNING):
            return await env.controller.handle_message(event("hi"))

    assert asyncio.run(main()) == {"action": "skip", "reason": "relic-onboarding"}
    assert "OSError" in caplog.text


def test_hook_exception_for_unauthorized_user_returns_none(tmp_path):
    env = Env(tmp_path)

    def boom(*a, **k):
        raise OSError("disk")

    env.controller._allowed_users = boom
    assert asyncio.run(env.controller.handle_message(event("/start"))) is None


def test_missing_chat_type_passes_through(tmp_path):
    env = Env(tmp_path)
    ev = event("/start")
    del ev.source.chat_type
    assert asyncio.run(env.controller.handle_message(ev)) is None
    assert env.bot.sent == []


def test_start_with_payload_is_not_an_answer(tmp_path):
    env = Env(tmp_path)

    async def main():
        await drive_to(env, "name")
        await env.controller.handle_message(event("/start xyz"))

    asyncio.run(main())
    assert "name" not in env.progress()["answers"]
    assert env.current().id == "name"
