"""Telegram onboarding controller, hook and button handler (no real Telegram)."""

from __future__ import annotations

import pytest

from tests.secret_fakes import seed_credentials

pytestmark = pytest.mark.usefixtures("scoped_secrets")
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace


from relic.hermes_plugin import telegram_onboarding as tg
from relic.onboarding.engine import Engine
from relic.onboarding.questions import _WELCOME_EMOJI, build_questions
from relic.profile.registry import ProfileRegistry

TOKEN = "111111111:" + "A" * 35  # fake, built at runtime
USER = "123456789"
CHAT = "123456789"

QUICK = {
    "mode": "default",
    "img": "no",
    "aud": "no",
    "mus": "no",
    "armed": "no",
    "c_act": "no",
    "c_pas": "no",
}


# --- fakes -----------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return SimpleNamespace(message_id=1)


def fake_markup(qid, labels, *, skip=False):
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": f"rg:{qid}:{'skip' if skip else i}"}]
            for i, label in enumerate(labels)
        ]
    }


class FakeQuery:
    def __init__(self, data, user_id=USER, chat_id=CHAT, chat_type="private"):
        self.data = data
        self.from_user = SimpleNamespace(id=int(user_id))
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=int(chat_id), type=chat_type), message_id=1
        )
        self.answers: list[tuple] = []
        self.edits: list[dict] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


def event(text, user_id=USER, chat_id=CHAT, chat_type="dm", platform="telegram"):
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            chat_id=chat_id,
            user_id=user_id,
            chat_type=chat_type,
        ),
    )


class Env:
    def __init__(self, tmp_path: Path, **overrides):
        self.relic_home = tmp_path / "relic"
        self.profiles_home = tmp_path / "hermes" / "profiles"
        self.activations: list[int] = []
        self.bot = FakeBot()
        env = {"TELEGRAM_ALLOWED_USERS": USER, "TELEGRAM_BOT_TOKEN": TOKEN}
        env.update(overrides)
        seed_credentials(env)
        self.controller = tg.OnboardingController(
            relic_home=self.relic_home,
            profiles_home=self.profiles_home,
            hermes_base_home=tmp_path / "hermes",
            env=env,
            bot=self.bot,
            activate=lambda: self.activations.append(1),
            markup=fake_markup,
        )

    @property
    def progress_path(self) -> Path:
        return self.relic_home / "user" / "onboarding_progress.json"

    def progress(self) -> dict:
        return json.loads(self.progress_path.read_text(encoding="utf-8"))

    def current(self):
        answers = self.progress()["answers"] if self.progress_path.exists() else {}
        return Engine(build_questions(), answers).current()

    def last_buttons(self) -> list[list[dict]]:
        return self.bot.sent[-1]["reply_markup"]["inline_keyboard"]

    async def start(self, text="/start"):
        return await self.controller.handle_message(event(text))

    async def tap(self, data, **kwargs):
        query = FakeQuery(data, **kwargs)
        await self.controller.on_callback(SimpleNamespace(callback_query=query), None)
        return query

    async def tap_value(self, qid, value):
        question = next(q for q in build_questions() if q.id == qid)
        index = [o.value for o in question.options].index(value)
        return await self.tap(f"rg:{qid}:{index}")

    async def run_quick(self, confirm="save"):
        await self.start()
        await self.tap_value("notice", "ok")
        for _ in range(200):
            question = self.current()
            if question is None or self.progress()["status"] == "complete":
                return
            if question.kind == "text":
                if question.id == "tz":
                    await self.controller.handle_message(event("Europe/Rome"))
                else:
                    await self.tap(f"rg:{question.id}:skip")
            elif question.id == "confirm":
                await self.tap_value("confirm", confirm)
                return
            elif question.id in QUICK:
                await self.tap_value(question.id, QUICK[question.id])
            else:
                await self.tap_value(question.id, question.options[0].value)
        raise AssertionError("onboarding did not reach the end")


def sent_texts(bot: FakeBot) -> list[str]:
    return [str(call.get("text", "")) for call in bot.sent]


# --- hook: pass-through rules ----------------------------------------------


def test_unauthorized_user_passes_through_and_writes_nothing(tmp_path):
    env = Env(tmp_path)

    async def main():
        return await env.controller.handle_message(event("/start", user_id="999"))

    assert asyncio.run(main()) is None
    assert env.bot.sent == []
    assert not env.relic_home.exists()


def test_group_chat_passes_through(tmp_path):
    env = Env(tmp_path)
    result = asyncio.run(env.controller.handle_message(event("/start", chat_type="group")))
    assert result is None
    assert env.bot.sent == []


def test_other_commands_pass_through_but_start_runs(tmp_path):
    env = Env(tmp_path)

    async def main():
        assert await env.start("/help") is None
        assert env.bot.sent == []
        return await env.start("/start")

    assert asyncio.run(main()) == {"action": "skip", "reason": "relic-onboarding"}
    assert env.bot.sent


def test_other_platform_and_missing_bot_or_token_pass_through(tmp_path):
    env = Env(tmp_path)
    assert (
        asyncio.run(env.controller.handle_message(event("/start", platform="discord")))
        is None
    )
    env.controller.bot = None
    assert asyncio.run(env.start()) is None
    no_token = Env(tmp_path / "b", TELEGRAM_BOT_TOKEN="")
    assert asyncio.run(no_token.start()) is None
    assert no_token.bot.sent == []


def test_profile_env_is_never_a_credential_source(tmp_path):
    env = Env(tmp_path, TELEGRAM_ALLOWED_USERS="", TELEGRAM_BOT_TOKEN="")
    env_file = env.profiles_home / HERMES_USER_PROFILE_NAME / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        f"TELEGRAM_ALLOWED_USERS=1,{USER}\nTELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8"
    )
    os.chmod(env_file, 0o600)
    assert asyncio.run(env.start()) is None
    assert env.bot.sent == []


# --- first contact and questions -------------------------------------------


def test_first_questions_are_the_notice_then_name(tmp_path):
    env = Env(tmp_path)
    result = asyncio.run(env.start())
    assert result == {"action": "skip", "reason": "relic-onboarding"}
    assert len(env.bot.sent) == 1
    call = env.bot.sent[0]
    assert call["chat_id"] == int(CHAT)
    assert call["text"] == (
        "Before we start: I'm Gumi, a fictional character. No person reads "
        "this chat. I'm not a medical or clinical service. In an emergency, "
        "call 112 or your local emergency services."
    )
    rows = env.last_buttons()
    assert all(len(row) == 1 for row in rows)
    assert rows[0][0]["callback_data"] == "rg:notice:0"
    assert rows[0][0]["text"] == "Continue"
    saved = env.progress()
    assert saved["chat_id"] == CHAT and saved["user_id"] == USER
    assert saved["answers"] == {}


def test_second_message_greets_like_a_person_and_asks_the_name(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        call = env.bot.sent[-1]

        assert call["text"] == f"Nice to meet you! I'm Gumi {_WELCOME_EMOJI}, who are you?"
        assert call["text"] == "Nice to meet you! I'm Gumi 👋, who are you?"
        assert "Reply with text" not in call["text"]
        rows = env.last_buttons()
        assert all(len(row) == 1 for row in rows)
        assert rows[0][0]["callback_data"] == "rg:name:skip"
        assert rows[0][0]["text"] == "Skip"
        return await env.controller.handle_message(event("Ada"))

    asyncio.run(main())
    assert env.progress()["answers"] == {"notice": "ok", "name": "Ada"}
    assert env.current().id == "tz"


@pytest.mark.parametrize("first", ["/start", "hi", "Ada"])
def test_first_contact_text_is_never_taken_as_the_name(tmp_path, first):
    env = Env(tmp_path)

    async def main():
        await env.start(first)
        await env.tap_value("notice", "ok")
        assert env.progress()["answers"] == {"notice": "ok"}
        assert env.current().id == "name"
        await env.start("Ada")

    asyncio.run(main())
    assert env.progress()["answers"] == {"notice": "ok", "name": "Ada"}
    assert env.current().id == "tz"


def test_old_progress_file_with_unknown_ids_resumes_without_crashing(tmp_path):
    env = Env(tmp_path)
    env.progress_path.parent.mkdir(parents=True)
    env.progress_path.write_text(
        json.dumps(
            {
                "status": "in_progress",
                "chat_id": CHAT,
                "user_id": USER,
                "answers": {"welcome": "start", "tz": "Europe/Rome", "b01": "3", "b48": "4"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(env.progress_path, 0o600)
    result = asyncio.run(env.start("/start"))
    assert result == {"action": "skip", "reason": "relic-onboarding"}
    assert env.progress()["answers"] == {"tz": "Europe/Rome"}
    assert env.bot.sent and env.last_buttons()[0][0]["callback_data"] == "rg:notice:0"


def test_partial_v1_progress_without_catalog_drops_battery_answers(tmp_path):
    env = Env(tmp_path)
    env.progress_path.parent.mkdir(parents=True)
    answers = {"notice": "ok", "name": "Ada", "tz": "Europe/Rome", "mode": "default"}
    answers.update({f"b{n:02d}": "3" for n in range(1, 21)})
    env.progress_path.write_text(
        json.dumps({"status": "in_progress", "chat_id": CHAT, "user_id": USER, "answers": answers}),
        encoding="utf-8",
    )
    os.chmod(env.progress_path, 0o600)
    asyncio.run(env.start("/start"))
    saved = env.progress()
    assert not any(k.startswith("b") and k[1:].isdigit() for k in saved["answers"])
    assert saved["catalog"] == "telegram-v2"


def test_progress_records_the_catalog_version_and_keeps_answers_on_resume(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.start("Ada")
        await env.start("Europe/Rome")
        await env.tap_value("mode", "default")
        await env.tap_value("b01", "3")
        await env.start("/start")

    asyncio.run(main())
    saved = env.progress()
    assert saved["catalog"] == "telegram-v2"
    assert saved["answers"].get("b01") == "3"


def test_free_text_to_choice_question_repeats_it(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.start("Ada")
        await env.start("Europe/Rome")
        return await env.start("hello there")

    assert asyncio.run(main()) == {"action": "skip", "reason": "relic-onboarding"}
    assert any("Please use the buttons." in text for text in sent_texts(env.bot))
    assert env.last_buttons()[0][0]["callback_data"] == "rg:mode:0"
    assert env.progress()["answers"] == {"notice": "ok", "name": "Ada", "tz": "Europe/Rome"}


def test_stale_button_is_expired_and_does_not_advance(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.tap("rg:name:skip")
        before = (dict(env.progress()["answers"]), len(env.bot.sent))
        query = await env.tap("rg:name:skip")
        return before, query

    before, query = asyncio.run(main())
    assert query.answers == [("Expired", {})]
    assert (env.progress()["answers"], len(env.bot.sent)) == before
    assert env.current().id == "tz"


def test_timezone_validation(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.tap("rg:name:skip")
        await env.controller.handle_message(event("Mars/Olympus"))
        invalid = (dict(env.progress()["answers"]), env.current().id)
        invalid_texts = list(sent_texts(env.bot))
        await env.controller.handle_message(event(" Europe/Rome "))
        return invalid, invalid_texts

    invalid, invalid_texts = asyncio.run(main())
    assert invalid[1] == "tz" and "tz" not in invalid[0]
    assert any("time zone" in text for text in invalid_texts[-2:])
    assert env.progress()["answers"]["tz"] == "Europe/Rome"
    assert env.current().id == "mode"
    assert env.last_buttons()[0][0]["callback_data"] == "rg:mode:0"
    assert env.last_buttons()[0][0]["text"] == "Quick (recommended)"


def test_battery_shows_progress_and_chosen_answer_replaces_keyboard(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        await env.tap("rg:name:skip")
        await env.controller.handle_message(event("Europe/Rome"))
        await env.tap_value("mode", "default")
        return await env.tap_value("b01", "1")

    query = asyncio.run(main())
    assert "(1/37)" in "".join(sent_texts(env.bot))
    assert "(2/37)" in sent_texts(env.bot)[-1]
    assert len(env.last_buttons()) == 7
    assert query.edits and "reply_markup" not in query.edits[0]
    assert query.edits[0]["text"].endswith("> Strongly disagree")
    assert query.answers == [(None, {})]


def test_invalid_option_index_is_expired(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        return await env.tap("rg:name:9")

    query = asyncio.run(main())
    assert query.answers == [("Expired", {})]
    assert env.progress()["answers"] == {"notice": "ok"}


# --- callbacks: authorization ----------------------------------------------


def test_callback_always_answers_even_when_not_allowed(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        await env.tap_value("notice", "ok")
        denied = await env.tap("rg:name:skip", user_id="999")
        group = await env.tap("rg:name:skip", chat_type="group")
        garbage = await env.tap("rg:garbage")
        ok = await env.tap("rg:name:skip")
        return denied, group, garbage, ok

    denied, group, garbage, ok = asyncio.run(main())
    assert denied.answers == [("Not allowed", {})]
    assert group.answers == [("Not allowed", {})]
    assert garbage.answers == [("Expired", {})]
    assert ok.answers == [(None, {})]
    assert env.progress()["answers"] == {"notice": "ok", "name": "__skip__"}


# --- completion -------------------------------------------------------------


def test_complete_onboarding_passes_through_always(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.run_quick()
        sent = len(env.bot.sent)
        results = [
            await env.start("/start"),
            await env.start("hello"),
            await env.start("/help"),
        ]
        return sent, results

    sent, results = asyncio.run(main())
    assert results == [None, None, None]
    assert len(env.bot.sent) == sent
    assert env.progress()["status"] == "complete"


def test_confirm_save_finalizes_once_and_activates(tmp_path, monkeypatch):
    calls = {"assemble": 0, "finalize": 0}
    real_assemble, real_finalize = tg.assemble, tg.finalize

    def counting_assemble(*args, **kwargs):
        calls["assemble"] += 1
        assert kwargs["telegram_token"] == TOKEN and kwargs["chat_id"] == CHAT
        return real_assemble(*args, **kwargs)

    def counting_finalize(*args, **kwargs):
        calls["finalize"] += 1
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(tg, "assemble", counting_assemble)
    monkeypatch.setattr(tg, "finalize", counting_finalize)
    env = Env(tmp_path)

    async def main():
        await env.run_quick()
        return await env.tap("rg:confirm:0")

    again = asyncio.run(main())
    assert again.answers == [("Expired", {})]
    assert calls == {"assemble": 1, "finalize": 1}
    assert env.activations == [1]
    assert env.progress()["status"] == "complete"
    assert "All set" in sent_texts(env.bot)[-1]


def test_confirm_shows_summary_without_sensitive_values(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.start()
        for _ in range(200):
            question = env.current()
            if question.id == "confirm":
                return
            if question.kind == "text":
                if question.id == "tz":
                    await env.controller.handle_message(event("Europe/Rome"))
                else:
                    await env.tap(f"rg:{question.id}:skip")
            else:
                await env.tap_value(
                    question.id, QUICK.get(question.id, question.options[0].value)
                )

    asyncio.run(main())
    text = sent_texts(env.bot)[-1]
    assert "__summary__" not in text
    assert "Europe/Rome" in text
    assert TOKEN not in text
    assert [b[0]["text"] for b in env.last_buttons()] == ["Save", "Start over"]


def test_confirm_restart_clears_answers(tmp_path):
    env = Env(tmp_path)

    async def main():
        await env.run_quick(confirm="restart")

    asyncio.run(main())
    saved = env.progress()
    assert saved["answers"] == {}
    assert saved["chat_id"] == CHAT and saved["user_id"] == USER
    assert saved["status"] != "complete"
    assert env.last_buttons()[0][0]["callback_data"] == "rg:notice:0"
    assert env.activations == []


def test_activation_failure_keeps_complete_and_tells_user(tmp_path):
    env = Env(tmp_path)

    def boom():
        raise RuntimeError(f"cron exploded with {TOKEN}")

    env.controller.activate = boom
    asyncio.run(env.run_quick())
    saved = env.progress()
    assert saved["status"] == "complete"
    assert saved["activation_error"] == "RuntimeError"
    assert "Saved, but scheduling failed" in sent_texts(env.bot)[-1]
    assert TOKEN not in json.dumps(saved)


def test_activation_failure_log_keeps_sanitized_cause_only(tmp_path, caplog):
    from relic.user_runtime import RuntimeActivationError

    env = Env(tmp_path)

    def boom():
        raise RuntimeActivationError("cron activation failed: cron create returned no job id")

    env.controller.activate = boom
    with caplog.at_level("WARNING"):
        asyncio.run(env.run_quick())
    assert "cron create returned no job id" in caplog.text
    assert TOKEN not in caplog.text

    other = Env(tmp_path / "other")

    def leak():
        raise RuntimeError(f"cron exploded with {TOKEN}")

    other.controller.activate = leak
    caplog.clear()
    with caplog.at_level("WARNING"):
        asyncio.run(other.run_quick())
    assert "activation failed: RuntimeError" in caplog.text
    assert "exploded" not in caplog.text and TOKEN not in caplog.text


def test_token_is_only_in_profile_env(tmp_path):
    env = Env(tmp_path)
    asyncio.run(env.run_quick())
    env_file = env.profiles_home / HERMES_USER_PROFILE_NAME / ".env"
    holders = []
    for root in (env.relic_home, env.profiles_home):
        for path in root.rglob("*"):
            if path.is_file() and TOKEN in path.read_text(encoding="utf-8", errors="ignore"):
                holders.append(path)
    assert holders == []
    assert not env_file.exists()
    for call in env.bot.sent + env.bot.edits:
        assert TOKEN not in json.dumps(call, default=str)


def test_full_quick_path_produces_valid_canonical_config(tmp_path):
    env = Env(tmp_path)
    asyncio.run(env.run_quick())
    registry = ProfileRegistry(
        relic_home=env.relic_home, hermes_profiles_home=env.profiles_home
    )
    config = registry.load_user_configuration()
    assert config is not None
    assert config["baseline"]["timezone"] == "Europe/Rome"
    assert len(config["baseline"]["battery"]["responses"]) >= 48
    paths = registry._artifact_paths()
    assert len(paths) == 5
    for path in paths.values():
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    consents = json.loads(
        (env.relic_home / "user" / "checkin" / "delivery_policy.json").read_text()
    )
    assert consents["consent_for_active_elicitation"] is False
    assert consents["consent_for_passive_extraction"] is False
    assert env.activations == [1]


# --- wiring ----------------------------------------------------------------


class FakeApplication:
    def __init__(self):
        self.bot = FakeBot()
        self.handlers: list = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


def test_wire_application_is_idempotent_without_sdk(tmp_path):
    env = Env(tmp_path)
    env.controller.bot = None
    holder = tg.BotHolder()
    application = FakeApplication()
    made = []

    def make_handler(callback, pattern):
        made.append((callback, pattern))
        return ("handler", pattern)

    for _ in range(2):
        tg.wire_application(
            application, object(), controller=env.controller, holder=holder,
            make_handler=make_handler,
        )
    assert application.handlers == [(("handler", r"^rg:"), 0)]
    assert made == [(env.controller.on_callback, r"^rg:")]
    assert env.controller.bot is application.bot is holder.bot
    other = FakeApplication()
    tg.wire_application(
        other, object(), controller=env.controller, holder=holder, make_handler=make_handler
    )
    assert len(other.handlers) == 1


def test_wire_application_real_handler_is_pattern_scoped(tmp_path):
    ptb = pytest.importorskip("telegram.ext")
    env = Env(tmp_path)
    env.controller.bot = None
    holder = tg.BotHolder()
    application = FakeApplication()
    tg.wire_application(application, object(), controller=env.controller, holder=holder)
    tg.wire_application(application, object(), controller=env.controller, holder=holder)
    assert len(application.handlers) == 1
    handler, group = application.handlers[0]
    assert isinstance(handler, ptb.CallbackQueryHandler)
    assert handler.pattern.pattern == r"^rg:"
    assert group == 0
    assert env.controller.bot is application.bot
    assert holder.bot is application.bot


def test_default_markup_is_vertical():
    pytest.importorskip("telegram")
    markup = tg._markup("b01", ["A", "B", "C"])
    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [1, 1, 1]
    assert [row[0].callback_data for row in rows] == ["rg:b01:0", "rg:b01:1", "rg:b01:2"]
    skip = tg._markup("name", ["Skip"], skip=True)
    assert skip.inline_keyboard[0][0].callback_data == "rg:name:skip"


def test_register_onboarding_registers_hook_and_platform_handler(tmp_path):
    registered = {"hooks": [], "platform": []}

    class Ctx:
        def register_hook(self, name, callback):
            registered["hooks"].append((name, callback))

        def register_platform_handler(self, platform, factory):
            registered["platform"].append((platform, factory))

    controller = tg.register_onboarding(
        Ctx(),
        relic_home=tmp_path / "relic",
        profiles_home=tmp_path / "profiles",
        hermes_base_home=tmp_path,
        env={},
    )
    assert registered["hooks"] == [("pre_gateway_dispatch", controller.handle_message)]
    assert [p for p, _ in registered["platform"]] == ["telegram"]


def test_register_onboarding_survives_host_without_platform_handler(tmp_path):
    registered = []

    class OldCtx:
        def register_hook(self, name, callback):
            registered.append(name)

    tg.register_onboarding(
        OldCtx(),
        relic_home=tmp_path / "relic",
        profiles_home=tmp_path / "profiles",
        hermes_base_home=tmp_path,
        env={},
    )
    assert registered == ["pre_gateway_dispatch"]
