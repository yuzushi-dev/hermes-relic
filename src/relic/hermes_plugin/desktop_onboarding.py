"""Desktop onboarding: ``/gumi-setup`` with one native card per question.

The command returns immediately and asks the questions from a detached thread
(``contextvars.copy_context()`` carries the asker): a synchronous handler would
hit the renderer's 30 s ``slash.exec`` timeout and be relaunched twice. All the
question texts come from the shared catalog (``relic.onboarding.presentation``);
this module adds no wording of its own except the fallback-B list.

State is the same ``ProgressStore`` file Telegram uses, guarded by a per-session
in-process lock and a run id, so a relaunch while the thread is alive cannot
double-run the battery. The final message is the last card (``post_message``
from the thread is unavailable on the Desktop).
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from relic.onboarding.assemble import assemble, finalize
from relic.onboarding.battery_text import BATTERY_PRESENTATION
from relic.onboarding.channel import (
    activation_detail as _activation_detail,
    default_activate as _default_activate,
    env_value as _env_value,
    is_complete as _is_complete_progress,
)
from relic.onboarding.engine import SKIP, Engine
from relic.onboarding.presentation import SUMMARY_PROMPT, question_text, summary
from relic.onboarding.questions import build_questions
from relic.onboarding.store import ProgressStore
from relic.profile.registry import ProfileRegistry

logger = logging.getLogger(__name__)

_DESKTOP_CONTROLLER: "DesktopOnboardingController | None" = None

# Surfaces where ``ctx.ask_choice`` shows a native picker (Hermes docs, "Ask the
# user to pick an option": Classic CLI, Ink TUI and Desktop use the clarify
# picker; ``local`` is a CLI/Desktop session owned by the gateway). These run
# the ask_choice battery. Telegram keeps its own flow; every other platform is
# told where onboarding is available. cron, subagent, curator and
# gateway_hygiene are deliberately not in the set.
INTERACTIVE_LOCAL_PLATFORMS = frozenset({"desktop", "tui", "cli", "local"})
TELEGRAM_PLATFORM = "telegram"


def routed_setup_command(
    desktop_controller: "DesktopOnboardingController",
) -> Callable[..., str]:
    """One ``/gumi-setup`` for every surface: route on the dispatcher platform.

    The plugin-command dispatcher passes ``source``, ``session_id``,
    ``session_key`` and ``platform`` to handlers that declare them.
    ``INTERACTIVE_LOCAL_PLATFORMS`` (desktop, tui, cli, local) run the
    ask_choice battery; Telegram (``platform`` ``telegram`` or absent, the
    historical behavior) keeps its exact flow; any other platform gets a short
    pointer and nothing is started or written. The Telegram controller is
    resolved lazily because the routed handler is registered before
    ``register_onboarding`` creates it.
    """

    def handler(raw_args: str = "", **kwargs: Any) -> str:
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform in INTERACTIVE_LOCAL_PLATFORMS:
            return desktop_controller.setup_command(
                raw_args,
                session_key=str(kwargs.get("session_key") or ""),
                platform=platform,
            )
        if platform and platform != TELEGRAM_PLATFORM:
            return UNSUPPORTED_PLATFORM_REPLY
        from relic.hermes_plugin import telegram_onboarding

        controller = telegram_onboarding._CONTROLLER
        if controller is None:  # Telegram registration failed: fail soft
            return ""
        return controller.setup_command(raw_args)

    return handler

# Fallback B for the time zone when the free-text asker is unavailable: a
# fixed list of common zones (no personal data). "Other" stays a valid typed
# answer only through the private asker; without it the user picks from here.
COMMON_ZONES = (
    "Europe/Rome",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Paris",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Tokyo",
)

STARTED_REPLY = (
    "Onboarding started: answer the cards as they come. "
    "You can leave it and resume with /gumi-setup."
)
ALREADY_RUNNING_REPLY = "Onboarding already running in this session: answer the cards."
RETRY_STARTED_REPLY = (
    "Retrying the scheduling step: a card will tell you how it went."
)
RETRY_NOT_READY_REPLY = "The setup is not complete yet: run /gumi-setup first."
NOTHING_TO_RETRY_REPLY = "Nothing to retry."
RETRY_FAILED_TEXT = "Scheduling failed again. Run `relic-gumi verify`."
ALL_SET_TEXT = "All set. You can talk to me now."
NOT_INTERACTIVE_REPLY = ""  # routing decides; the command itself never sees this
UNSUPPORTED_PLATFORM_REPLY = (
    "Onboarding is available on Telegram and in the Hermes Desktop/CLI/TUI chat."
)
BLOCK_MESSAGE = (
    "I still don't know you: the setup is not done. "
    "Run /gumi-setup and answer the cards, then we can talk."
)


def free_text_asker() -> Callable[[str, list], str | None] | None:
    """The ONLY place that touches Hermes's private free-text asker.

    ``hermes_cli.plugin_choices._ASKER`` is not public API: it may move or
    disappear at any Hermes upgrade. Callers must treat ``None`` as "unavailable"
    and degrade to fallback B. A dedicated test pins this seam so the breakage
    is loud.
    """
    try:
        from hermes_cli import plugin_choices  # protected import, private API

        asker = plugin_choices._ASKER.get()
    except Exception:  # noqa: BLE001 - import or contextvar may vanish
        return None
    return asker if callable(asker) else None


class DesktopOnboardingController:
    """Thread-safe Desktop adapter over Engine + ProgressStore + assemble."""

    def __init__(
        self,
        *,
        relic_home: Path,
        profiles_home: Path,
        hermes_base_home: Path,
        env: Mapping[str, str],
        ask_choice: Callable[[str, list], str | None],
        free_asker: Callable[[], Callable[[str, list], str | None] | None] = free_text_asker,
        activate: Callable[[], Any] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.relic_home = Path(relic_home)
        self.profiles_home = Path(profiles_home)
        self.hermes_base_home = Path(hermes_base_home)
        self.env = env
        self.ask_choice = ask_choice  # ctx.ask_choice, bound at command time
        self.free_asker = free_asker
        self.activate = activate or _default_activate(self.relic_home, self.hermes_base_home)
        self.clock = clock or (lambda: uuid.uuid4().hex)
        self._runs: dict[str, str] = {}  # session key -> run id (per-process guard)
        self._runs_lock = threading.Lock()
        # Serializes read-check-write on the progress file across runs of this
        # process (sessions are guarded separately; ``desktop_run`` decides who
        # may write).
        self._state_lock = threading.Lock()
        # Serializes the scheduling step (``activate``) across runs and sessions:
        # two concurrent activations would reconcile the same empty job list and
        # create duplicate jobs. Order: activation lock first, state lock inside.
        # Cards are never shown while either lock is held.
        self._activation_lock = threading.Lock()

    # -- configuration ------------------------------------------------------

    def _registry(self) -> ProfileRegistry:
        return ProfileRegistry(
            relic_home=self.relic_home, hermes_profiles_home=self.profiles_home
        )

    def _store(self) -> ProgressStore:
        return ProgressStore(self.relic_home / "user")

    def _value(self, name: str) -> str:
        return _env_value(self.env, self._registry(), name)

    def is_complete(self) -> bool:
        """True when the setup is done. Raises ``OSError``/``ValueError`` when the
        progress file is unreadable: that is "unknown", not "incomplete", and
        ``first_turn_block`` turns it into fail-open."""
        return _is_complete_progress(self._registry(), self._store().load())

    def first_turn_block(self, kwargs: Mapping[str, Any]) -> dict[str, str] | None:
        """Block the first message of an interactive local surface while the
        setup is not complete.

        Returns the ``pre_llm_call`` block payload (spike H4) only for a first
        turn on ``INTERACTIVE_LOCAL_PLATFORMS`` (desktop, tui, cli, local);
        Telegram, cron, subagent and any other platform are never touched.
        Fail-open: a broken check must never brick the chat.
        """
        try:
            platform = str(kwargs.get("platform") or "").strip().lower()
            if platform not in INTERACTIVE_LOCAL_PLATFORMS or not kwargs.get("is_first_turn"):
                return None
            if self.is_complete():
                return None
            return {"action": "block", "message": BLOCK_MESSAGE}
        except Exception:  # noqa: BLE001 - fail open (unreadable state included)
            logger.warning("relic onboarding first-turn check failed: letting the turn through")
            return None

    # -- command ------------------------------------------------------------

    def setup_command(
        self, raw_args: str = "", *, session_key: str = "", platform: str = ""
    ) -> str:
        """Return immediately; ask the battery in a detached thread.

        ``session_key``/``platform`` arrive from the plugin-command dispatcher
        (``invoke_plugin_command``); the per-session guard keys on them.
        """
        if platform and platform not in INTERACTIVE_LOCAL_PLATFORMS:
            # Not ours: the shared /gumi-setup routes here only for the
            # interactive local surfaces.
            return NOT_INTERACTIVE_REPLY
        retry = raw_args.strip().lower() == "retry"
        if retry:
            early = self._retry_precheck()
            if early is not None:
                return early
        key = session_key or "<no-session>"
        with self._runs_lock:
            if key in self._runs:
                return ALREADY_RUNNING_REPLY
            run_id = self.clock()
            self._runs[key] = run_id
        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run,
            args=(self._run_retry if retry else self._run_battery, run_id, key),
            daemon=True,
            name=f"relic-desktop-onboarding-{run_id[:8]}",
        )
        thread.start()
        return RETRY_STARTED_REPLY if retry else STARTED_REPLY

    def _retry_precheck(self) -> str | None:
        """None when an activation retry is possible, else the reply to give."""
        try:
            progress = self._store().load()
            complete = _is_complete_progress(self._registry(), progress)
        except (OSError, ValueError):
            logger.warning("relic onboarding progress unreadable")
            return RETRY_NOT_READY_REPLY
        if not complete:
            return RETRY_NOT_READY_REPLY
        if not progress.get("activation_error"):
            return NOTHING_TO_RETRY_REPLY
        return None

    def _release(self, key: str) -> None:
        with self._runs_lock:
            self._runs.pop(key, None)

    # -- battery ------------------------------------------------------------

    def _run_battery(self, run_id: str, session_key: str) -> None:
        try:
            store = self._store()
            with self._state_lock:
                try:
                    progress = store.load()
                except (OSError, ValueError):
                    logger.warning("relic onboarding progress unreadable")
                    return
                if _is_complete_progress(self._registry(), progress):
                    # Redo: same semantics as the Telegram /gumi-setup restart.
                    progress["answers"] = {}
                    progress["status"] = "in_progress"
                    progress.pop("activation_error", None)
                    progress.pop("desktop_run", None)
                progress.setdefault("status", "in_progress")
                if progress["status"] == "new":
                    progress["status"] = "in_progress"
                if progress.get("catalog") != BATTERY_PRESENTATION:
                    # Answers collected with another catalog: the Engine drops the
                    # battery ones (ids were reused for other questions), exactly
                    # like Telegram, BEFORE the current version is stamped.
                    progress["answers"] = Engine(
                        build_questions(), progress["answers"], progress.get("catalog")
                    ).answers
                progress["catalog"] = BATTERY_PRESENTATION
                # The newest run takes ownership: an older thread still blocked
                # on an open card finds a different id and stops without writing.
                progress["desktop_run"] = run_id
                store.save(progress)
            while True:
                progress = store.load()
                if progress.get("desktop_run") != run_id:
                    logger.info("relic desktop onboarding run superseded")
                    return
                engine = Engine(build_questions(), progress["answers"], progress.get("catalog"))
                question = engine.current()
                if question is None:
                    break
                answer = self._ask(question, engine)
                if answer is None:
                    # Tab closed / card dismissed / timeout: stop clean, keep state.
                    logger.info("relic desktop onboarding stopped at %s", question.id)
                    return
                with self._state_lock:
                    # A card can stay open for the whole clarify timeout (1 h by
                    # default): meanwhile a newer /gumi-setup (another session, or
                    # this one after a close/reopen) may own the state. Only the
                    # run that wrote ``desktop_run`` last may write again.
                    progress = store.load()
                    if progress.get("desktop_run") != run_id:
                        logger.info("relic desktop onboarding run superseded at %s", question.id)
                        return
                    engine = Engine(build_questions(), progress["answers"], progress.get("catalog"))
                    current = engine.current()
                    if current is None or current.id != question.id:
                        continue  # state moved under us: re-ask what is current
                    if question.id == "confirm" and answer == "restart":
                        # "Start over": same semantics as the Telegram confirm/restart.
                        engine.restart()
                        progress["answers"] = {}
                        progress["status"] = "in_progress"
                        progress.pop("activation_error", None)
                        store.save(progress)
                        continue
                    result = engine.answer(question.id, answer)
                    if not result.ok:
                        # Invalid for the engine (should not happen: values come from
                        # the card); re-ask by not saving.
                        continue
                    progress["answers"] = engine.answers
                    progress["catalog"] = BATTERY_PRESENTATION
                    progress["desktop_run"] = run_id
                    store.save(progress)
            self._finalize(engine, store, run_id)
        except Exception as exc:  # noqa: BLE001 - a thread crash must never surface raw
            logger.warning(
                "relic desktop onboarding failed: %s%s",
                type(exc).__name__,
                _activation_detail(exc),
            )
        finally:
            self._release(session_key)

    def _ask(self, question: Any, engine: Engine) -> str | None:
        """One native card per question; free text through the private asker."""
        text = question_text(question, engine)
        if question.kind == "choice":
            # Dict choices: ask_choice returns the catalog VALUE, not the label.
            choices = [{"value": option.value, "label": option.label} for option in question.options]
            return self.ask_choice(text, choices)
        # Free-text question: try the private asker, else fallback B.
        try:
            asker = self.free_asker()
        except Exception:  # noqa: BLE001 - the seam must degrade, never crash
            asker = None
        if asker is None:
            return self._fallback_b(question)
        try:
            answer = asker(text, [])
        except Exception:  # noqa: BLE001 - the API broke mid-call: degrade, never crash
            return self._fallback_b(question)
        if answer is None:
            return None  # cancelled / tab closed / timeout: stop clean, no extra card
        if answer.strip():
            return answer
        return self._fallback_b(question)  # blank text: offer the skip/list card

    def _fallback_b(self, question: Any) -> str | None:
        if question.id == "name":
            picked = self.ask_choice(question.prompt, [question.skip_label or "Skip"])
            return SKIP if picked is not None else None
        if question.id == "tz":
            picked = self.ask_choice(question.prompt, list(COMMON_ZONES))
            return picked
        # Generic fallback for any other open question: its skip label, if any.
        if question.skip_label:
            picked = self.ask_choice(question.prompt, [question.skip_label])
            return SKIP if picked is not None else None
        return None

    def _finalize(self, engine: Engine, store: ProgressStore, run_id: str) -> None:
        """Commit the canonical files, show the last card, schedule.

        Every write happens under the state lock and only while ``run_id`` still
        owns the state: a run superseded while its last card was open writes
        nothing (neither the canonical files nor ``activation_error``).
        """
        with self._state_lock:
            progress = store.load()
            if progress.get("desktop_run") != run_id:
                logger.info("relic desktop onboarding run superseded before finalize")
                return
            registry = self._registry()
            assembled = assemble(
                engine.answers,
                telegram_token=self._value("TELEGRAM_BOT_TOKEN"),
                chat_id=self._value("TELEGRAM_HOME_CHANNEL") or self._value("TELEGRAM_ALLOWED_USERS"),
                gemini_key=self._value("GEMINI_API_KEY"),
                language=None,
            )
            finalize(registry, registry.initialize_user(), assembled)
            progress["answers"] = engine.answers
            progress["status"] = "complete"
            store.save(progress)
        # The final message is the last card: post_message is unavailable
        # from the detached thread on the Desktop (spike H2). No lock is held
        # while the card is open.
        self.ask_choice(summary(engine.answers) + "\n\n" + ALL_SET_TEXT, ["Fine"])
        with self._activation_lock:
            with self._state_lock:
                if store.load().get("desktop_run") != run_id:
                    logger.info("relic desktop onboarding run superseded before activation")
                    return
            try:
                self.activate()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "relic onboarding activation failed: %s%s",
                    type(exc).__name__,
                    _activation_detail(exc),
                )
                with self._state_lock:
                    progress = store.load()
                    if progress.get("desktop_run") == run_id:
                        progress["activation_error"] = type(exc).__name__
                        store.save(progress)

    def _run_retry(self, run_id: str, session_key: str) -> None:
        """Re-run only the scheduling step; the answers and ``complete`` stay.

        The whole check-activate-record sequence runs under the activation lock:
        a second retry (any session) waits, then finds the error already cleared
        and does nothing, instead of scheduling the same jobs twice.
        """
        try:
            store = self._store()
            with self._activation_lock:
                with self._state_lock:
                    progress = store.load()
                    if not (_is_complete_progress(self._registry(), progress)
                            and progress.get("activation_error")):
                        return  # the state moved since the precheck
                    progress["desktop_run"] = run_id
                    store.save(progress)
                try:
                    self.activate()
                    failed = False
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    logger.warning(
                        "relic onboarding activation failed: %s%s",
                        type(exc).__name__,
                        _activation_detail(exc),
                    )
                if not failed:
                    with self._state_lock:
                        progress = store.load()
                        if progress.get("desktop_run") == run_id:
                            progress.pop("activation_error", None)
                            store.save(progress)
            self.ask_choice(RETRY_FAILED_TEXT if failed else ALL_SET_TEXT, ["Fine"])
        except Exception as exc:  # noqa: BLE001 - a thread crash must never surface raw
            logger.warning("relic desktop onboarding retry failed: %s", type(exc).__name__)
        finally:
            self._release(session_key)


def register_desktop_onboarding(
    ctx: Any,
    *,
    relic_home: Path,
    profiles_home: Path,
    hermes_base_home: Path,
    env: Mapping[str, str],
) -> "DesktopOnboardingController":
    """Wire the Desktop side: first-turn block hook + the controller factory.

    The command itself is NOT re-registered here: the Telegram registration
    owns ``/gumi-setup``; the plugin registers it once with the routed handler
    (``routed_setup_command``), chosen by the dispatcher's ``platform`` kwarg.
    ``ctx.ask_choice`` is bound at command time by the surface, so the
    controller resolves the asker per call (``_ctx_ask_choice``).
    """
    global _DESKTOP_CONTROLLER
    controller = DesktopOnboardingController(
        relic_home=relic_home,
        profiles_home=profiles_home,
        hermes_base_home=hermes_base_home,
        env=env,
        ask_choice=_ctx_ask_choice,
    )
    _DESKTOP_CONTROLLER = controller
    # The first-turn block is NOT registered as its own hook: the plugin keeps a
    # single ``pre_llm_call`` and calls ``controller.first_turn_block`` from it
    # (directory_plugin._register_hooks), so the hook count stays one per name.
    return controller


def _ctx_ask_choice(question: str, choices: list) -> str | None:
    """``ctx.ask_choice`` resolved at call time (bound per command dispatch)."""
    from hermes_cli.plugin_choices import ask_choice

    return ask_choice(question, choices)
