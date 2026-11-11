"""Desktop onboarding controller: thread, resume, fallback B, parity.

No real Desktop, no real Telegram: the asker is a fake that records every
card and answers from a script, like the Telegram suite does.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relic.hermes_plugin import desktop_onboarding as desk
from relic.onboarding.engine import SKIP, Engine
from relic.onboarding.questions import build_questions
from relic.profile.registry import ProfileRegistry

TOKEN = "111111111:" + "A" * 35  # fake, built at runtime
USER = "123456789"

QUICK = {
    "mode": "default",
    "img": "no",
    "aud": "no",
    "mus": "no",
    "armed": "no",
    "c_act": "no",
    "c_pas": "no",
}


class ScriptedAsker:
    """Fake ask_choice + free-text asker: records cards, answers from a queue."""

    def __init__(self, answers: list[str | None]):
        self.answers = list(answers)
        self.cards: list[dict[str, Any]] = []
        self.free_cards: list[str] = []
        self.lock = threading.Lock()
        self.free_enabled = True
        self.free_text: list[str | None] = []
        self.free_asker_calls = 0

    # ctx.ask_choice(question, choices) -> value or None
    def ask_choice(self, question: str, choices: list) -> str | None:
        with self.lock:
            self.cards.append({"question": question, "choices": list(choices)})
            if not self.answers:
                return None
            answer = self.answers.pop(0)
        if answer is None:
            return None
        # Mirror hermes_cli.plugin_choices.ask_choice: match value or label.
        for choice in choices:
            value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
            label = str(choice.get("label", value)) if isinstance(choice, dict) else value
            if answer in (value, label) or answer == question:
                return value
        return None

    def free_asker(self):
        self.free_asker_calls += 1
        if not self.free_enabled:
            return None
        return self._free

    def _free(self, question: str, _labels: list) -> str | None:
        with self.lock:
            self.free_cards.append(question)
            if not self.free_text:
                return None
            return self.free_text.pop(0)


class Env:
    def __init__(self, tmp_path: Path, asker: ScriptedAsker, **env_overrides):
        self.relic_home = tmp_path / "relic"
        self.profiles_home = tmp_path / "hermes" / "profiles"
        self.activations: list[int] = []
        env = {"TELEGRAM_ALLOWED_USERS": USER, "TELEGRAM_BOT_TOKEN": TOKEN}
        env.update(env_overrides)
        self.asker = asker
        self.controller = desk.DesktopOnboardingController(
            relic_home=self.relic_home,
            profiles_home=self.profiles_home,
            hermes_base_home=tmp_path / "hermes",
            env=env,
            ask_choice=asker.ask_choice,
            free_asker=asker.free_asker,
            activate=lambda: self.activations.append(1),
        )

    @property
    def progress_path(self) -> Path:
        return self.relic_home / "user" / "onboarding_progress.json"

    def progress(self) -> dict:
        return json.loads(self.progress_path.read_text(encoding="utf-8"))

    def wait(self, timeout: float = 10.0) -> None:
        # The battery runs in a detached thread; wait for the run guard to clear.
        import time as _t

        for _ in range(int(timeout * 20)):
            with self.controller._runs_lock:
                if not self.controller._runs:
                    return
            _t.sleep(0.05)
        raise TimeoutError("desktop onboarding thread did not finish")


def _quick_answers() -> dict[str, str]:
    values: dict[str, int] = {}
    from relic.profile._bootstrap_steps import item_battery as ib

    for index, item in enumerate(ib.USER_BASELINE_ITEMS):
        low, high, _ = ib._scale_bounds(item["response_scale"])
        values[item["item_id"]] = low + 2
    asked = {q.item_id for q in build_questions() if q.item_id}
    for item in ib.USER_BASELINE_ITEMS:
        if item["item_id"] not in asked:
            values[item["item_id"]] = ib._conservative_default(item)
    answers = {"name": SKIP, "tz": "Europe/Rome", "notice": "ok", "confirm": "save"}
    answers.update(QUICK)
    for question in build_questions():
        if question.item_id:
            answers[question.id] = str(values[question.item_id])
    return answers


def _script_for(answers: dict[str, str]) -> list[str]:
    """The card answers (values) in catalog order for the given answer map."""
    script: list[str] = []
    engine = Engine(build_questions(), {})
    while True:
        question = engine.current()
        if question is None:
            break
        raw = answers[question.id]
        if question.kind == "choice":
            script.append(raw)
        else:
            script.append(("FREE:", raw))
        result = engine.answer(question.id, SKIP if raw == SKIP else raw)
        assert result.ok, (question.id, result.error)
    return script


def _make_env(tmp_path: Path, answers: dict[str, str], **env_overrides) -> tuple[Env, ScriptedAsker]:
    script = _script_for(answers)
    free_texts = [a[1] for a in ((s,) for s in script) if a[0][0].startswith("FREE:")] if False else None
    # split: choice answers go to ask_choice, free texts to the free asker
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker, **env_overrides)
    return env, asker


def _run_full(tmp_path: Path, answers: dict[str, str], **env_overrides) -> tuple[Env, ScriptedAsker]:
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker, **env_overrides)
    engine = Engine(build_questions(), {})
    # Feed the asker lazily: pop answers as the controller asks.
    remaining = dict(answers)

    def ask_choice(question: str, choices: list) -> str | None:
        asker.cards.append({"question": question, "choices": list(choices)})
        # current question id from the text? Use the engine to find it.
        qid = _current_qid(env)
        if qid is None:
            return None
        raw = remaining.pop(qid, None)
        if raw is None:
            return None
        if raw == SKIP:
            # skip buttons carry the skip label as value
            for choice in choices:
                value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
                if value == SKIP:
                    return SKIP
            return None
        for choice in choices:
            value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
            if value == raw:
                return value
        return None

    def free_asker():
        asker.free_asker_calls += 1
        if not asker.free_enabled:
            return None

        def ask(question: str, _labels: list) -> str | None:
            asker.free_cards.append(question)
            qid = _current_qid(env)
            raw = remaining.pop(qid, "") if qid else ""
            return raw or None

        return ask

    env.controller.ask_choice = ask_choice
    env.controller.free_asker = free_asker
    env.controller.setup_command("", session_key="s1", platform="desktop")
    env.wait()
    return env, asker


def _current_qid(env: Env) -> str | None:
    try:
        progress = env.progress()
    except (OSError, ValueError):
        progress = {"answers": {}}
    engine = Engine(build_questions(), progress.get("answers", {}), progress.get("catalog"))
    question = engine.current()
    return question.id if question else None


# --- command dispatch ------------------------------------------------------


def test_setup_command_returns_immediately_and_spawns_thread(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    reply = env.controller.setup_command("", session_key="s1", platform="desktop")
    assert reply == desk.STARTED_REPLY
    with env.controller._runs_lock:
        assert "s1" in env.controller._runs
    env.controller._release("s1")


def test_setup_command_double_launch_same_session_guarded(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    first = env.controller.setup_command("", session_key="s1", platform="desktop")
    second = env.controller.setup_command("", session_key="s1", platform="desktop")
    assert first == desk.STARTED_REPLY
    assert second == desk.ALREADY_RUNNING_REPLY
    env.controller._release("s1")
    # a different session is independent
    other = env.controller.setup_command("", session_key="s2", platform="desktop")
    assert other == desk.STARTED_REPLY
    env.controller._release("s2")


def test_setup_command_not_desktop_platform_is_ignored(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    assert env.controller.setup_command("", session_key="s1", platform="telegram") == ""
    assert env.controller.setup_command("", session_key="s1", platform="cron") == ""
    with env.controller._runs_lock:
        assert not env.controller._runs


# --- full battery + parity --------------------------------------------------


def test_full_battery_produces_the_same_canonical_files_as_telegram(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)

    assert env.progress()["status"] == "complete"
    assert env.activations == [1]
    registry = env.controller._registry()
    artifacts = registry.load_user_artifacts()
    assert artifacts is not None
    # Same canonical files the Telegram flow writes (parity with the catalog):
    # compare against a Telegram-side assemble+finalize with the same answers.
    from relic.onboarding.assemble import assemble, finalize

    tg_registry = ProfileRegistry(
        relic_home=tmp_path / "tg" / "relic",
        hermes_profiles_home=tmp_path / "tg" / "hermes" / "profiles",
    )
    assembled = assemble(
        answers, telegram_token=TOKEN, chat_id=USER, language=None
    )
    finalize(tg_registry, tg_registry.initialize_user(), assembled)
    tg_art = tg_registry.load_user_artifacts()
    assert tg_art is not None

    def strip(artifact: dict) -> dict:
        # runtime_scope_id is random per registry (see test_assemble_parity).
        return {key: value for key, value in artifact.items() if key != "runtime_scope_id"}

    assert strip(artifacts["baseline_response"]) == strip(tg_art["baseline_response"])
    assert strip(artifacts["gumi_profile"]) == strip(tg_art["gumi_profile"])
    assert strip(artifacts["checkin_state"]) == strip(tg_art["checkin_state"])
    assert strip(artifacts["runtime_policy"]) == strip(tg_art["runtime_policy"])


def test_battery_counter_and_verbatim_texts_from_catalog(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)
    # First battery card carries the preamble and the (1/37) counter.
    battery_cards = [
        c for c in asker.cards if c["question"].startswith("(") or "\n\n(" in c["question"]
    ]
    assert battery_cards, "no battery counter found in any card"
    first = battery_cards[0]["question"]
    assert "(1/37)" in first
    # A TIPI item and an ECR-RS item appear verbatim.
    from relic.onboarding import battery_text

    texts = [c["question"] for c in asker.cards]
    joined = "\n".join(texts)
    tipi_prompt = next(
        entry.prompt
        for entry in battery_text.battery_prompts()
        if entry.item_id and entry.item_id.startswith("TIPI")
    )
    ecr_prompt = next(
        entry.prompt
        for entry in battery_text.battery_prompts()
        if entry.item_id and entry.item_id.startswith("ECR")
    )
    assert tipi_prompt in joined
    assert ecr_prompt in joined


def test_choice_cards_scale_to_seven_buttons(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)
    seven = [c for c in asker.cards if len(c["choices"]) == 7]
    assert seven, "no 7-button card was shown"
    # every battery card is one-per-item with 7 options
    battery_cards = [c for c in asker.cards if c["question"].lstrip().startswith("(")]
    assert all(len(c["choices"]) == 7 for c in battery_cards)


# --- resume -----------------------------------------------------------------


def test_resume_from_saved_state_skips_answered_questions(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)
    total_cards = len(asker.cards)

    # Simulate a stop mid-battery: reset progress to a partial state.
    progress = env.progress()
    partial_answers = dict(list(progress["answers"].items())[:5])
    progress["answers"] = partial_answers
    progress["status"] = "in_progress"
    env.controller._store().save(progress)

    env2, asker2 = _run_full(tmp_path, {**answers})
    # The second run must NOT re-ask the 5 answered questions.
    asked_ids = [c["question"] for c in asker2.cards]
    assert len(asker2.cards) < total_cards
    # the resumed first card is the 6th question of the catalog
    engine = Engine(build_questions(), partial_answers)
    expected_first = engine.current()
    assert expected_first is not None
    assert expected_first.id not in partial_answers


# --- asker None (tab closed) ------------------------------------------------


def test_asker_none_stops_clean_and_saves_state(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)

    calls = {"n": 0}

    def ask_choice(question: str, choices: list) -> str | None:
        asker.cards.append({"question": question, "choices": list(choices)})
        calls["n"] += 1
        if calls["n"] >= 3:
            return None  # the user closed the tab
        return "ok" if "Before we start" in question else "Continue"

    def free_asker():
        return None

    env.controller.ask_choice = ask_choice
    env.controller.free_asker = free_asker
    env.controller.setup_command("", session_key="s1", platform="desktop")
    env.wait()
    progress = env.progress()
    assert progress["status"] == "in_progress"
    assert progress["answers"]  # the answered questions were saved
    with env.controller._runs_lock:
        assert not env.controller._runs  # the guard was released


# --- fallback B --------------------------------------------------------------


def test_fallback_b_name_skip_and_tz_list_when_private_asker_missing(tmp_path):
    answers = _quick_answers()
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    env.controller.free_asker = lambda: None  # private asker unavailable

    remaining = dict(answers)

    def ask_choice(question: str, choices: list) -> str | None:
        asker.cards.append({"question": question, "choices": list(choices)})
        qid = _current_qid(env)
        raw = remaining.pop(qid, None) if qid else None
        if raw is None:
            return None
        if raw == SKIP:
            values = [str(c["value"]) if isinstance(c, dict) else str(c) for c in choices]
            if SKIP in values:
                return SKIP
            if len(values) == 1:  # fallback B: click the single skip button
                return values[0]
            return None
        for choice in choices:
            value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
            if value == raw:
                return value
        return None

    env.controller.ask_choice = ask_choice
    env.controller.setup_command("", session_key="s1", platform="desktop")
    env.wait()

    # name was asked as a choice with a Skip button; tz as the common-zone list
    name_cards = [c for c in asker.cards if "who are you" in c["question"]]
    assert name_cards and any("Skip" in str(c["choices"]) for c in name_cards)
    tz_cards = [c for c in asker.cards if "time zone" in c["question"].lower()]
    assert tz_cards
    assert all(zone in tz_cards[0]["choices"] for zone in desk.COMMON_ZONES)
    assert env.progress()["status"] == "complete"
    assert not asker.free_cards


def test_private_asker_used_for_name_and_tz_when_available(tmp_path):
    answers = _quick_answers()
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    remaining = dict(answers)

    def ask_choice(question: str, choices: list) -> str | None:
        asker.cards.append({"question": question, "choices": list(choices)})
        qid = _current_qid(env)
        raw = remaining.pop(qid, None) if qid else None
        if raw is None:
            return None
        for choice in choices:
            value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
            if value == raw:
                return value
        return None

    def free_asker():
        def ask(question: str, _labels: list) -> str | None:
            asker.free_cards.append(question)
            qid = _current_qid(env)
            return remaining.pop(qid, "") or None

        return ask

    env.controller.ask_choice = ask_choice
    env.controller.free_asker = free_asker
    env.controller.setup_command("", session_key="s1", platform="desktop")
    env.wait()
    # Both free-text questions went through the private asker
    assert len(asker.free_cards) == 2
    assert any("who are you" in q for q in asker.free_cards)
    assert any("time zone" in q.lower() for q in asker.free_cards)
    assert env.progress()["status"] == "complete"


def test_private_asker_exception_degrades_to_fallback_b(tmp_path):
    answers = _quick_answers()
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    remaining = dict(answers)

    def ask_choice(question: str, choices: list) -> str | None:
        asker.cards.append({"question": question, "choices": list(choices)})
        qid = _current_qid(env)
        raw = remaining.pop(qid, None) if qid else None
        if raw is None:
            return None
        if raw == SKIP:
            values = [str(c["value"]) if isinstance(c, dict) else str(c) for c in choices]
            if SKIP in values:
                return SKIP
            if len(values) == 1:  # fallback B: click the single skip button
                return values[0]
            return None
        for choice in choices:
            value = str(choice["value"]) if isinstance(choice, dict) else str(choice)
            if value == raw:
                return value
        return None

    def free_asker():
        def ask(question: str, _labels: list) -> str | None:
            raise RuntimeError("private asker broke")

        return ask

    env.controller.ask_choice = ask_choice
    env.controller.free_asker = free_asker
    env.controller.setup_command("", session_key="s1", platform="desktop")
    env.wait()
    # fallback B kicked in: name card with Skip, tz card with the zone list
    name_cards = [c for c in asker.cards if "who are you" in c["question"]]
    assert name_cards and any("Skip" in str(c["choices"]) for c in name_cards)
    tz_cards = [c for c in asker.cards if "time zone" in c["question"].lower()]
    assert tz_cards and all(z in tz_cards[0]["choices"] for z in desk.COMMON_ZONES)
    assert env.progress()["status"] == "complete"


# --- free_text_asker seam -----------------------------------------------------


def test_free_text_asker_isolated_and_degrades(monkeypatch):
    # The seam must return None (not raise) when the private API is missing.
    import sys

    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # import raises ImportError
    assert desk.free_text_asker() is None


def test_ask_degrades_when_injected_free_asker_raises(tmp_path):
    # The controller must treat a raising free_asker as "unavailable" (fallback B),
    # never as a crash: the battery keeps going through the choice cards.
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)

    def free_asker():
        raise ImportError("hermes_cli.plugin_choices vanished")

    env.controller.free_asker = free_asker
    # The user clicks whatever single button the fallback card offers.
    env.controller.ask_choice = lambda question, choices: (
        asker.cards.append({"question": question, "choices": list(choices)}), choices[0]
    )[1]
    question = next(q for q in build_questions() if q.id == "name")
    picked = env.controller._fallback_b(question)
    assert picked == SKIP
    # and _ask itself must not raise
    engine = Engine(build_questions(), {"notice": "ok"})
    assert env.controller._ask(question, engine) == SKIP


def test_free_text_asker_ignores_private_contextvar_even_when_bound(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("hermes_cli.plugin_choices")

    def ask(question, labels):
        return "typed answer"

    fake._ASKER = _FakeContextVar(ask)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_choices", fake)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    # Review point 3: private context bindings are no longer a supported seam.
    # A bound internal asker must never re-enable free-text on local surfaces.
    assert desk.free_text_asker() is None


class _FakeContextVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


# --- registration: routed command, first-turn block ------------------------


def test_registration_calls_public_context_ask_choice(tmp_path):
    seen = []
    ctx = SimpleNamespace(ask_choice=lambda question, choices: seen.append((question, choices)) or "chosen")
    controller = desk.register_desktop_onboarding(
        ctx, relic_home=tmp_path / "relic", profiles_home=tmp_path / "profiles",
        hermes_base_home=tmp_path / "hermes", env={},
    )
    assert controller.ask_choice("Synthetic question", ["chosen"]) == "chosen"
    assert seen == [("Synthetic question", ["chosen"])]
    # Lookup remains per call, but only through the public PluginContext method.
    ctx.ask_choice = lambda question, choices: None
    assert controller.ask_choice("Cancelled", ["chosen"]) is None


def test_routed_setup_command_routes_on_platform(tmp_path):
    from relic.hermes_plugin import telegram_onboarding as tg

    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    handler = desk.routed_setup_command(env.controller)

    # Desktop: the routed handler delegates to the Desktop controller
    assert handler("", platform="desktop", session_key="s1") == desk.STARTED_REPLY
    with env.controller._runs_lock:
        assert "s1" in env.controller._runs
    env.controller._release("s1")

    # Telegram: the Telegram controller answers (monkeypatched, no real bot)
    telegram_controller = SimpleNamespace(
        setup_command=lambda raw_args: f"telegram reply: {raw_args}"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tg, "_CONTROLLER", telegram_controller)
    try:
        assert handler("retry", platform="telegram") == "telegram reply: retry"
        # no platform kwarg (legacy call): still Telegram
        assert handler("x") == "telegram reply: x"
    finally:
        monkey.undo()


def test_routed_setup_command_fails_soft_without_telegram_controller(tmp_path):
    from relic.hermes_plugin import telegram_onboarding as tg

    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    handler = desk.routed_setup_command(env.controller)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tg, "_CONTROLLER", None)
    try:
        assert handler("", platform="telegram") == ""
    finally:
        monkey.undo()


@pytest.mark.parametrize("platform", sorted(desk.INTERACTIVE_LOCAL_PLATFORMS))
def test_first_turn_block_on_every_interactive_local_surface(tmp_path, platform):
    env = Env(tmp_path, ScriptedAsker([]))
    # no progress file yet: incomplete
    assert env.controller.first_turn_block(
        {"platform": platform, "is_first_turn": True}
    ) == {"action": "block", "message": desk.BLOCK_MESSAGE}
    # not the first turn: never blocked
    assert env.controller.first_turn_block({"platform": platform}) is None
    assert env.controller.first_turn_block(
        {"platform": platform, "is_first_turn": False}
    ) is None


@pytest.mark.parametrize(
    "platform",
    ["telegram", "discord", "slack", "cron", "subagent", "curator", "gateway_hygiene", "", None],
)
def test_first_turn_block_never_outside_the_interactive_set(tmp_path, platform):
    env = Env(tmp_path, ScriptedAsker([]))
    kwargs = {"is_first_turn": True}
    if platform is not None:
        kwargs["platform"] = platform
    assert env.controller.first_turn_block(kwargs) is None
    assert env.controller.first_turn_block({}) is None


def test_interactive_local_set_is_exactly_the_documented_one():
    assert desk.INTERACTIVE_LOCAL_PLATFORMS == {"desktop", "tui", "cli", "local"}


@pytest.mark.parametrize("platform", sorted(desk.INTERACTIVE_LOCAL_PLATFORMS))
def test_routed_command_starts_the_battery_on_every_interactive_surface(tmp_path, platform):
    from relic.hermes_plugin import telegram_onboarding as tg

    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    handler = desk.routed_setup_command(env.controller)
    telegram_controller = SimpleNamespace(
        setup_command=lambda raw_args: "Send /start to begin"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tg, "_CONTROLLER", telegram_controller)
    try:
        reply = handler("", platform=platform.upper(), session_key="s1")
    finally:
        monkey.undo()
    assert reply == desk.STARTED_REPLY
    assert "/start" not in reply and "Telegram" not in reply
    env.wait()
    assert asker.cards  # the battery asked at least one card through ask_choice


@pytest.mark.parametrize("platform", sorted(desk.INTERACTIVE_LOCAL_PLATFORMS))
def test_controller_setup_command_accepts_every_interactive_surface(tmp_path, platform):
    env = Env(tmp_path, ScriptedAsker([]))
    assert env.controller.setup_command(session_key="s", platform=platform) == desk.STARTED_REPLY
    env.wait()


@pytest.mark.parametrize("platform", ["telegram", None])
def test_routed_command_keeps_telegram_behavior(tmp_path, platform):
    from relic.hermes_plugin import telegram_onboarding as tg

    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    handler = desk.routed_setup_command(env.controller)
    telegram_controller = SimpleNamespace(
        setup_command=lambda raw_args: f"telegram reply: {raw_args}"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tg, "_CONTROLLER", telegram_controller)
    try:
        kwargs = {} if platform is None else {"platform": platform}
        assert handler("retry", **kwargs) == "telegram reply: retry"
    finally:
        monkey.undo()
    assert asker.cards == []
    assert not env.progress_path.exists()


def _state_hash(env) -> str | None:
    import hashlib

    if not env.progress_path.exists():
        return None
    return hashlib.sha256(env.progress_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("platform", ["discord", "slack", "matrix", "cron", "subagent"])
def test_routed_command_on_other_platforms_starts_nothing(tmp_path, platform):
    from relic.hermes_plugin import telegram_onboarding as tg

    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    handler = desk.routed_setup_command(env.controller)
    telegram_calls: list[str] = []
    telegram_controller = SimpleNamespace(
        setup_command=lambda raw_args: telegram_calls.append(raw_args) or "telegram"
    )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tg, "_CONTROLLER", telegram_controller)
    try:
        before = _state_hash(env)
        reply = handler("", platform=platform, session_key="s1")
        after = _state_hash(env)
    finally:
        monkey.undo()
    assert reply == desk.UNSUPPORTED_PLATFORM_REPLY
    assert "Telegram" in reply and "Desktop/CLI/TUI" in reply
    assert reply != desk.STARTED_REPLY
    assert telegram_calls == []
    assert asker.cards == []
    assert before == after
    with env.controller._runs_lock:
        assert not env.controller._runs
    assert not (env.relic_home / "user").exists()


@pytest.mark.parametrize("platform", ["discord", "cron", "subagent"])
def test_controller_setup_command_refuses_other_platforms(tmp_path, platform):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    assert env.controller.setup_command(session_key="s", platform=platform) == desk.NOT_INTERACTIVE_REPLY
    assert asker.cards == []
    with env.controller._runs_lock:
        assert not env.controller._runs


def test_first_turn_block_fails_open_when_broken(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)

    def boom():
        raise OSError("disk gone")

    env.controller.is_complete = boom
    assert env.controller.first_turn_block(
        {"platform": "desktop", "is_first_turn": True}
    ) is None


def test_register_desktop_onboarding_registers_no_hook_of_its_own(tmp_path):
    from types import SimpleNamespace as NS

    hooks = []

    ctx = NS(register_hook=lambda name, cb: hooks.append((name, cb)))
    controller = desk.register_desktop_onboarding(
        ctx,
        relic_home=tmp_path / "relic",
        profiles_home=tmp_path / "hermes" / "profiles",
        hermes_base_home=tmp_path / "hermes",
        env={},
    )
    assert desk._DESKTOP_CONTROLLER is controller
    # one pre_llm_call per plugin: the plugin's own hook calls first_turn_block
    assert hooks == []
    # the block callback is what the plugin's single hook delegates to
    assert controller.first_turn_block(
        {"platform": "desktop", "is_first_turn": True}
    ) == {"action": "block", "message": desk.BLOCK_MESSAGE}
    assert controller.first_turn_block(
        {"platform": "desktop", "is_first_turn": False}
    ) is None


# --- superseded run (card left open, newer /gumi-setup) ----------------------


def test_older_run_with_open_card_is_superseded_and_never_writes(tmp_path):
    """A card can stay open for the whole clarify timeout (1 h by default).

    Seen on the test Desktop: after closing and reopening the window a new
    /gumi-setup ran in a NEW session while the old thread was still blocked on
    its card. When that stale card is finally answered the old run must stop
    without touching the state the newer run owns.
    """
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    env.controller.free_asker = lambda: None

    release_old = threading.Event()
    old_waiting = threading.Event()
    owner = {"current": "old"}

    def ask_choice(question: str, choices: list) -> str | None:
        me = threading.current_thread().name
        if owner["current"] == "old" and "old" in me:
            old_waiting.set()
            release_old.wait(10)
            # The stale card is answered with a value valid for its question.
            value = choices[0]["value"] if isinstance(choices[0], dict) else choices[0]
            return str(value)
        return None  # the new run stops at its first card (state stays its own)

    env.controller.ask_choice = ask_choice
    ids = iter(["old-run-00000000", "new-run-00000000"])
    env.controller.clock = lambda: next(ids)
    env.controller.setup_command("", session_key="s-old", platform="desktop")
    assert old_waiting.wait(5)
    owner["current"] = "new"
    env.controller.setup_command("", session_key="s-new", platform="desktop")
    # wait for the new run to take ownership and stop at its first card
    for _ in range(200):
        if env.progress().get("desktop_run") == "new-run-00000000":
            break
        threading.Event().wait(0.02)
    before = env.progress()
    assert before["desktop_run"] == "new-run-00000000"
    release_old.set()
    env.wait()
    after = env.progress()
    assert after == before  # the stale answer was dropped, nothing rewritten


# --- review round 1 (Sol): ownership at finalize, catalog migration, retry, -----
# --- fail-open on unreadable state, cancel vs unavailable, contract test --------


def _complete_env(tmp_path, **kw):
    """An Env whose battery already ran once to ``complete`` (activation failed)."""
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker, **kw)
    answers = _quick_answers()
    env.controller._store().save(
        {"status": "in_progress", "catalog": desk.BATTERY_PRESENTATION, "answers": {
            k: v for k, v in answers.items() if k != "confirm"}}
    )
    return env, asker


def test_superseded_run_never_finalizes_or_writes_activation_error(tmp_path):
    env, _asker = _complete_env(tmp_path)
    c = env.controller
    old_at_final, release_old, new_at_card = (threading.Event() for _ in range(3))
    ids = iter(["old-run-00000000", "new-run-00000000"])
    c.clock = lambda: next(ids)

    def ask(question, choices):
        if "old-run" in threading.current_thread().name:
            if choices == ["Fine"]:
                old_at_final.set()
                assert release_old.wait(10)
                return "Fine"
            return "save"
        new_at_card.set()
        return None

    def failing_activation():
        raise RuntimeError("scheduling failed")

    c.ask_choice, c.activate = ask, failing_activation
    assert c.setup_command(session_key="old", platform="desktop") == desk.STARTED_REPLY
    assert old_at_final.wait(10)
    assert c.setup_command(session_key="new", platform="desktop") == desk.STARTED_REPLY
    assert new_at_card.wait(10)
    before = env.progress()
    assert before["desktop_run"] == "new-run-00000000" and before["answers"] == {}
    release_old.set()
    env.wait()
    assert env.progress() == before  # the old run wrote nothing after the new one took over


def test_old_catalog_battery_answers_are_dropped_not_relabelled(tmp_path):
    for catalog in (None, "telegram-v1"):
        asker = ScriptedAsker([])
        env = Env(tmp_path / str(catalog), asker)
        saved = {"notice": "ok", "name": SKIP, "tz": "Europe/Rome", "mode": "default", "b01": "3"}
        progress = {"status": "in_progress", "answers": dict(saved)}
        if catalog is not None:
            progress["catalog"] = catalog
        env.controller._store().save(progress)
        expected = Engine(build_questions(), saved, catalog).current()
        assert expected.id == "b01"
        env.controller.setup_command(session_key="m", platform="desktop")
        env.wait()
        first = asker.cards[0]["question"]
        assert "(1/37)" in first, first  # restarted from the first item, like Telegram
        assert env.progress()["catalog"] == desk.BATTERY_PRESENTATION
        assert "b01" not in env.progress()["answers"] or env.progress()["answers"]["b01"] != "3"


def test_retry_reruns_only_the_activation_and_keeps_the_answers(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)
    assert env.progress()["status"] == "complete"
    store = env.controller._store()
    progress = store.load()
    progress["activation_error"] = "RuntimeError"
    store.save(progress)
    before_answers = dict(env.progress()["answers"])
    env.activations.clear()
    cards: list[tuple[str, list]] = []
    env.controller.ask_choice = lambda q, ch: (cards.append((q, list(ch))), "Fine")[1]
    reply = env.controller.setup_command("retry", session_key="r", platform="desktop")
    assert reply == desk.RETRY_STARTED_REPLY
    env.wait()
    after = env.progress()
    assert env.activations == [1]  # activation re-run exactly once
    assert after["status"] == "complete" and after["answers"] == before_answers
    assert "activation_error" not in after
    assert cards and cards[-1][1] == ["Fine"] and "All set" in cards[-1][0]


def test_retry_that_fails_again_keeps_the_error_and_the_answers(tmp_path):
    answers = _quick_answers()
    env, asker = _run_full(tmp_path, answers)
    store = env.controller._store()
    progress = store.load()
    progress["activation_error"] = "RuntimeError"
    store.save(progress)
    cards: list[str] = []
    env.controller.ask_choice = lambda q, ch: (cards.append(q), "Fine")[1]

    def boom():
        raise RuntimeError("still broken")

    env.controller.activate = boom
    env.controller.setup_command("retry", session_key="r", platform="desktop")
    env.wait()
    after = env.progress()
    assert after["status"] == "complete" and after["activation_error"] == "RuntimeError"
    assert after["answers"] and "relic-gumi verify" in cards[-1]


def test_retry_without_a_pending_error_or_setup_does_nothing(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    assert env.controller.setup_command("retry", session_key="r", platform="desktop") == desk.RETRY_NOT_READY_REPLY
    answers = _quick_answers()
    env2, _ = _run_full(tmp_path / "done", answers)
    assert env2.progress()["status"] == "complete" and "activation_error" not in env2.progress()
    assert env2.controller.setup_command("retry", session_key="r", platform="desktop") == desk.NOTHING_TO_RETRY_REPLY
    with env2.controller._runs_lock:
        assert not env2.controller._runs


def test_first_turn_block_fails_open_on_a_corrupt_progress_file(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    env.progress_path.parent.mkdir(parents=True)
    env.progress_path.write_text("{broken JSON", encoding="utf-8")
    kwargs = {"platform": "desktop", "is_first_turn": True}
    assert env.controller.first_turn_block(kwargs) is None  # unreadable != incomplete
    env.progress_path.unlink()
    assert env.controller.first_turn_block(kwargs) == {"action": "block", "message": desk.BLOCK_MESSAGE}


def test_free_text_cancel_stops_the_run_instead_of_opening_a_fallback_card(tmp_path):
    asker = ScriptedAsker([])
    env = Env(tmp_path, asker)
    fallback_cards: list[str] = []
    env.controller.free_asker = lambda: (lambda question, choices: None)  # resolved, user cancelled
    env.controller.ask_choice = lambda q, ch: (fallback_cards.append(q), ch[0])[1]
    question = next(q for q in build_questions() if q.id == "name")
    assert env.controller._ask(question, Engine(build_questions(), {"notice": "ok"})) is None
    assert fallback_cards == []  # no extra card after a cancel/timeout
    # an unavailable asker (missing or raising) still degrades to fallback B
    env.controller.free_asker = lambda: None
    assert env.controller._ask(question, Engine(build_questions(), {"notice": "ok"})) == SKIP
    assert len(fallback_cards) == 1


def test_bound_hermes_asker_does_not_enable_private_free_text():
    plugin_choices = pytest.importorskip("hermes_cli.plugin_choices")
    seen = []

    def fake(question, labels):
        seen.append((question, labels))
        return "typed"

    assert desk.free_text_asker() is None  # nothing bound outside a command
    with plugin_choices.bind_asker(fake):
        assert desk.free_text_asker() is None
    assert seen == []


def test_concurrent_retries_from_two_sessions_schedule_once(tmp_path):
    """Review round 2: two sessions retrying at once must not both run activate()."""
    env, _asker = _run_full(tmp_path, _quick_answers())
    store = env.controller._store()
    progress = store.load()
    progress["activation_error"] = "RuntimeError"
    store.save(progress)

    inside, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    state = {"calls": 0, "running": 0, "max_running": 0}

    def slow_activation():
        with guard:
            state["calls"] += 1
            state["running"] += 1
            state["max_running"] = max(state["max_running"], state["running"])
        inside.set()
        assert release.wait(10)
        with guard:
            state["running"] -= 1

    class _WatchedLock:
        """The real lock, plus an event set when a second thread tries to take it."""

        def __init__(self, inner):
            self._inner, self.contended = inner, threading.Event()

        def __enter__(self):
            if not self._inner.acquire(blocking=False):
                self.contended.set()  # someone already holds it: this is B waiting
                self._inner.acquire()
            return self

        def __exit__(self, *exc):
            self._inner.release()

    watched = _WatchedLock(env.controller._activation_lock)
    env.controller._activation_lock = watched
    env.controller.activate = slow_activation
    env.controller.ask_choice = lambda q, ch: "Fine"
    assert env.controller.setup_command("retry", session_key="a", platform="desktop") == desk.RETRY_STARTED_REPLY
    assert inside.wait(10)
    # the error is still recorded, so the second session passes the precheck
    assert env.controller.setup_command("retry", session_key="b", platform="desktop") == desk.RETRY_STARTED_REPLY
    # B is provably contending (or, on the old code, already inside activate())
    for _ in range(200):
        if watched.contended.is_set() or state["running"] > 1:
            break
        threading.Event().wait(0.05)
    release.set()
    env.wait()
    assert state["max_running"] == 1
    assert state["calls"] == 1  # B woke up after A cleared the error: nothing to do
    assert "activation_error" not in env.progress()
    assert env.progress()["status"] == "complete"
