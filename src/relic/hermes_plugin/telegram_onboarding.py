"""Telegram onboarding: ``pre_gateway_dispatch`` hook plus inline-button handler.

The conversation is a deterministic state machine (``relic.onboarding``); the LLM is
never involved. Free text arrives through the ``pre_gateway_dispatch`` hook, button
taps through a ``CallbackQueryHandler`` wired into the adapter's PTB application via
``register_platform_handler``. The ``telegram`` package is imported only inside
functions so the plugin loads without the SDK.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from relic.onboarding.assemble import assemble, finalize
from relic.onboarding.battery_text import BATTERY_PRESENTATION
from relic.onboarding.channel import (
    activation_detail as _activation_detail,
    default_activate as _default_activate,
    env_value as _env_value,
    is_complete as _is_complete_progress,
)
from relic.onboarding.engine import SKIP, Engine
from relic.onboarding.presentation import SUMMARY_PROMPT as _SUMMARY_PROMPT
from relic.onboarding.presentation import question_text as _question_text
from relic.onboarding.presentation import summary as _summary_text
from relic.onboarding.questions import Question, build_questions
from relic.onboarding.store import ProgressStore
from relic.profile.registry import ProfileRegistry

logger = logging.getLogger(__name__)

_SKIP_RESULT = {"action": "skip", "reason": "relic-onboarding"}
_CALLBACK_PATTERN = r"^rg:"


def _markup(qid: str, labels: Sequence[str], *, skip: bool = False) -> Any:
    """One button per row (vertical layout)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"rg:{qid}:{'skip' if skip else i}")]
            for i, label in enumerate(labels)
        ]
    )


class OnboardingController:
    def __init__(
        self,
        *,
        relic_home: Path,
        profiles_home: Path,
        hermes_base_home: Path,
        env: Mapping[str, str],
        bot: Any = None,
        activate: Callable[[], Any] | None = None,
        markup: Callable[..., Any] | None = None,
    ) -> None:
        self.relic_home = Path(relic_home)
        self.profiles_home = Path(profiles_home)
        self.hermes_base_home = Path(hermes_base_home)
        self.env = env
        self.bot = bot
        self.activate = activate or _default_activate(self.relic_home, self.hermes_base_home)
        self.markup = markup or _markup
        self._lock = asyncio.Lock()
        self._env_warned = False

    # -- configuration ------------------------------------------------------

    def _registry(self) -> ProfileRegistry:
        return ProfileRegistry(
            relic_home=self.relic_home, hermes_profiles_home=self.profiles_home
        )

    def _store(self) -> ProgressStore:
        return ProgressStore(self.relic_home / "user")

    def _value(self, name: str) -> str:
        def warn() -> None:
            if not self._env_warned:
                self._env_warned = True
                logger.warning(
                    "relic onboarding disabled: profile .env is not private (0600) or unreadable"
                )

        return _env_value(self.env, self._registry(), name, warn=warn)

    def _allowed_users(self) -> set[str]:
        return {u.strip() for u in self._value("TELEGRAM_ALLOWED_USERS").split(",") if u.strip()}

    def _is_complete(self, progress: Mapping[str, Any]) -> bool:
        return _is_complete_progress(self._registry(), progress)

    # -- /gumi-setup --------------------------------------------------------

    def setup_command(self, raw_args: str = "") -> str:
        """Restart onboarding; credentials and the saved config stay until re-saved."""
        store = self._store()
        if raw_args.strip().lower() == "retry":
            return self._retry_activation(store)
        try:
            progress = store.load()
            if not self._is_complete(progress):
                return "Send /start to begin"
            progress["answers"] = {}
            progress["status"] = "in_progress"
            progress["restarted"] = True
            progress.pop("activation_error", None)
            progress.pop("message_id", None)
            store.save(progress)
        except (OSError, ValueError):
            logger.warning("relic onboarding progress unreadable")
            return "Send /start to begin"
        return "Onboarding restarted. Send any message to begin."

    def _retry_activation(self, store: ProgressStore) -> str:
        """Re-run only the scheduling step after a saved-but-not-scheduled onboarding.

        The command handler is synchronous, so the (blocking, subprocess-based)
        activation runs inline: ``asyncio.run`` would fail inside the host's running
        event loop, and the call is short and operator-initiated.
        """
        try:
            progress = store.load()
        except (OSError, ValueError):
            logger.warning("relic onboarding progress unreadable")
            return "Send /start to begin"
        if not self._is_complete(progress):
            return "Send /start to begin"
        if not progress.get("activation_error"):
            return "Nothing to retry."
        try:
            self.activate()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "relic onboarding activation failed: %s%s",
                type(exc).__name__,
                _activation_detail(exc),
            )
            return "Scheduling failed again. Run `relic-gumi verify`."
        try:
            progress.pop("activation_error", None)
            store.save(progress)
        except OSError:
            logger.warning("relic onboarding progress unreadable")
        return "All set. You can talk to me now."

    # -- hook ---------------------------------------------------------------

    async def handle_message(self, event: Any = None, gateway: Any = None, session_store: Any = None, **_: Any) -> dict | None:
        destined = False  # True once the message is known to belong to the onboarding
        try:
            source = getattr(event, "source", None)
            if source is None:
                return None
            if getattr(getattr(source, "platform", None), "value", None) != "telegram":
                return None
            chat_type = getattr(source, "chat_type", None)
            if chat_type is None or str(getattr(chat_type, "value", chat_type)) != "dm":
                return None
            user_id = str(getattr(source, "user_id", ""))
            if user_id not in self._allowed_users():
                return None
            text = (getattr(event, "text", "") or "").strip()
            if text.startswith("/") and text.split()[0] != "/start":
                return None
            if not self._value("TELEGRAM_BOT_TOKEN"):
                logger.warning("relic onboarding disabled: TELEGRAM_BOT_TOKEN is not configured")
                return None
            if self.bot is None:
                return None
            store = self._store()
            try:
                if self._is_complete(store.load()):
                    return None
            except (OSError, ValueError):
                logger.warning("relic onboarding progress unreadable")
                return None
            destined = True
            async with self._lock:
                handled = await self._advance(
                    store, str(getattr(source, "chat_id", "")), user_id, text
                )
            return dict(_SKIP_RESULT) if handled else None
        except Exception as exc:  # noqa: BLE001 - a hook failure must never reach the LLM
            logger.warning("relic onboarding hook failed: %s", type(exc).__name__)
            return dict(_SKIP_RESULT) if destined else None

    async def _advance(
        self, store: ProgressStore, chat_id: str, user_id: str, text: str
    ) -> bool:
        """Process one message under the lock; False means "not ours, pass it on"."""
        progress = store.load()
        # Re-checked under the lock: a Save may have completed while we waited for it.
        if self._is_complete(progress):
            return False
        first_contact = not progress.get("chat_id")
        if not first_contact and (
            str(progress.get("chat_id", "")) != chat_id
            or str(progress.get("user_id", "")) != user_id
        ):
            return False  # another allowlisted user: never answer for the owner
        restarted = bool(progress.pop("restarted", False))  # set by /gumi-setup
        if first_contact:
            progress["chat_id"], progress["user_id"] = chat_id, user_id
        progress.setdefault("status", "in_progress")
        if progress["status"] == "new":
            progress["status"] = "in_progress"
        engine = Engine(build_questions(), progress["answers"], progress.get("catalog"))
        question = engine.current()
        if question is None:
            if progress["status"] == "complete":
                # stale "complete" without artifacts: start over
                engine.restart()
                progress["answers"] = {}
                progress["status"] = "in_progress"
            else:
                # everything answered but never finalized: show the summary again
                engine.answers.pop("confirm", None)
            question = engine.current()
        is_start = bool(text) and text.split()[0] == "/start"
        notice = ""
        if text and not is_start and not first_contact and not restarted:
            if question.kind == "choice":
                notice = "Please use the buttons."
            else:
                result = engine.answer(question.id, text)
                if result.ok:
                    progress["answers"] = engine.answers
                else:
                    notice = result.error
        progress["answers"] = engine.answers
        progress["catalog"] = BATTERY_PRESENTATION
        store.save(progress)
        try:
            if notice:
                await self._send(progress, notice)
            await self._present(store, progress, engine)
        except Exception as exc:  # noqa: BLE001 - answers are saved; log the class only
            logger.warning("relic onboarding send failed: %s", type(exc).__name__)
        return True

    # -- sending ------------------------------------------------------------

    async def _send(self, progress: dict, text: str, markup: Any = None) -> Any:
        kwargs: dict[str, Any] = {"chat_id": int(progress["chat_id"]), "text": text}
        if markup is not None:
            kwargs["reply_markup"] = markup
        return await self.bot.send_message(**kwargs)

    def _summary(self, answers: Mapping[str, str]) -> str:
        return _summary_text(answers)

    def _question_text(self, question: Question, engine: Engine) -> str:
        return _question_text(question, engine)

    async def _present(self, store: ProgressStore, progress: dict, engine: Engine) -> None:
        question = engine.current()
        if question is None:
            return
        text = self._question_text(question, engine)
        markup = None
        if question.kind == "choice":
            markup = self.markup(question.id, [o.label for o in question.options])
        elif question.skip_label:
            markup = self.markup(question.id, [question.skip_label], skip=True)
        sent = await self._send(progress, text, markup)
        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            progress["message_id"] = message_id
            store.save(progress)

    # -- callbacks ----------------------------------------------------------

    async def on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        answered = False

        async def reply(text: str | None = None) -> None:
            nonlocal answered
            answered = True
            try:
                await query.answer(text) if text else await query.answer()
            except Exception:  # noqa: BLE001 - never fail on a spinner
                logger.warning("relic onboarding: answerCallbackQuery failed")

        try:
            await self._on_callback(query, reply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("relic onboarding callback failed: %s", type(exc).__name__)
        finally:
            if not answered:
                await reply()

    async def _on_callback(self, query: Any, reply: Callable[..., Any]) -> None:
        message = getattr(query, "message", None)
        chat = getattr(message, "chat", None)
        user_id = str(getattr(getattr(query, "from_user", None), "id", ""))
        if user_id not in self._allowed_users() or str(getattr(chat, "type", "")) != "private":
            await reply("Not allowed")
            return
        parts = str(getattr(query, "data", "") or "").split(":")
        if len(parts) != 3 or parts[0] != "rg" or self.bot is None:
            await reply("Expired")
            return
        _, qid, arg = parts
        store = self._store()
        async with self._lock:
            progress = store.load()
            if self._is_complete(progress) or str(progress.get("chat_id", "")) != str(
                getattr(chat, "id", "")
            ):
                await reply("Expired")
                return
            engine = Engine(build_questions(), progress["answers"], progress.get("catalog"))
            question = engine.current()
            if question is None or question.id != qid:
                await reply("Expired")
                return
            if arg == "skip":
                if not question.skip_label:
                    await reply("Expired")
                    return
                value, label = SKIP, question.skip_label
            else:
                try:
                    option = question.options[int(arg)]
                except (ValueError, IndexError):
                    await reply("Expired")
                    return
                value, label = option.value, option.label
            prompt = (
                self._summary(engine.answers) if question.prompt == _SUMMARY_PROMPT else question.prompt
            )
            result = engine.answer(qid, value)
            if not result.ok:
                await reply("Expired")
                return
            await reply()
            await self._edit(query, f"{prompt}\n\n> {label}")
            if qid == "confirm":
                await self._confirm(store, progress, engine, value)
                return
            progress["answers"] = engine.answers
            progress["catalog"] = BATTERY_PRESENTATION
            store.save(progress)
            await self._present(store, progress, engine)

    async def _edit(self, query: Any, text: str) -> None:
        try:
            await query.edit_message_text(text=text)
        except Exception:  # noqa: BLE001
            logger.warning("relic onboarding: edit_message_text failed")

    async def _confirm(self, store: ProgressStore, progress: dict, engine: Engine, value: str) -> None:
        if value == "restart":
            engine.restart()
            progress["answers"] = {}
            progress["status"] = "in_progress"
            progress.pop("activation_error", None)
            progress["catalog"] = BATTERY_PRESENTATION
            store.save(progress)
            await self._present(store, progress, engine)
            return
        try:
            registry = self._registry()
            assembled = assemble(
                engine.answers,
                telegram_token=self._value("TELEGRAM_BOT_TOKEN"),
                chat_id=str(progress["chat_id"]),
                gemini_key=self._value("GEMINI_API_KEY"),
                language=None,
            )
            finalize(registry, registry.initialize_user(), assembled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("relic onboarding save failed: %s", type(exc).__name__)
            del engine.answers["confirm"]
            await self._send(progress, "I could not save your setup. Run `relic-gumi verify` and try again.")
            await self._present(store, progress, engine)
            return
        progress["answers"] = engine.answers
        progress["status"] = "complete"
        store.save(progress)
        try:
            await asyncio.to_thread(self.activate)
        except Exception as exc:  # noqa: BLE001
            progress["activation_error"] = type(exc).__name__
            store.save(progress)
            detail = _activation_detail(exc)
            logger.warning("relic onboarding activation failed: %s%s", type(exc).__name__, detail)
            await self._send(progress, "Saved, but scheduling failed. Run `relic-gumi verify`.")
            return
        await self._send(progress, "All set. You can talk to me now.")


class BotHolder:
    """Remembers the connected bot and which PTB applications are already wired."""

    def __init__(self) -> None:
        self.bot: Any = None
        self.wired: set[int] = set()


_HOLDER = BotHolder()
_CONTROLLER: OnboardingController | None = None


def wire_application(
    application: Any,
    adapter: Any,
    *,
    controller: OnboardingController,
    holder: BotHolder | None = None,
    make_handler: Callable[[Callable[..., Any], str], Any] | None = None,
) -> None:
    """Hermes platform-handler factory body: idempotent per application."""
    holder = holder or _HOLDER
    if id(application) in holder.wired:
        return
    if make_handler is None:
        from telegram.ext import CallbackQueryHandler

        make_handler = lambda callback, pattern: CallbackQueryHandler(  # noqa: E731
            callback, pattern=pattern
        )
    holder.bot = application.bot
    controller.bot = application.bot
    application.add_handler(make_handler(controller.on_callback, _CALLBACK_PATTERN))
    holder.wired.add(id(application))


def register_onboarding(
    ctx: Any,
    *,
    relic_home: Path,
    profiles_home: Path,
    hermes_base_home: Path,
    env: Mapping[str, str],
    callback_module: str | None = None,
    command_handler: Callable[[str], str] | None = None,
) -> OnboardingController:
    """Register the Telegram onboarding; ``command_handler`` optionally wraps
    ``/gumi-setup`` (the Desktop router) without touching its Telegram behavior."""
    global _CONTROLLER
    controller = OnboardingController(
        relic_home=relic_home,
        profiles_home=profiles_home,
        hermes_base_home=hermes_base_home,
        env=env,
    )
    _CONTROLLER = controller
    hook: Callable[..., Any] = controller.handle_message
    if callback_module:
        # The installer attributes callbacks to the plugin by ``__module__``, which a
        # bound method cannot override: expose a plain coroutine function instead.
        async def pre_gateway_dispatch(**kwargs: Any) -> dict | None:
            return await controller.handle_message(**kwargs)

        pre_gateway_dispatch.__module__ = callback_module
        hook = pre_gateway_dispatch
    ctx.register_hook("pre_gateway_dispatch", hook)
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(
            "gumi-setup",
            command_handler or controller.setup_command,
            description="Resume or restart Gumi onboarding",
        )
    else:
        logger.warning("host has no register_command: /gumi-setup is unavailable")
    register_platform = getattr(ctx, "register_platform_handler", None)
    if callable(register_platform):
        register_platform(
            "telegram",
            lambda native, adapter: wire_application(native, adapter, controller=controller),
        )
    else:
        logger.warning(
            "host has no register_platform_handler: Telegram onboarding buttons are inactive"
        )
    return controller
