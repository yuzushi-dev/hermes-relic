"""Plugin registration of the Telegram onboarding (hook, platform handler, command)."""

from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from relic.hermes_plugin import directory_plugin, install
from relic.hermes_plugin import telegram_onboarding as tg
from relic.profile.registry import ProfileRegistry

BASE_HOOKS = {"pre_llm_call", "post_llm_call", "transform_llm_output"}


def _dependencies():
    provider = Mock()
    provider.prefetch.return_value = "continuity"
    return SimpleNamespace(
        memory_provider=Mock(return_value=provider),
        inject_context=Mock(return_value={"context": "pcp"}),
        pending_ack_instruction=Mock(return_value="ack"),
        resolve_hermes_home=Mock(return_value=Path("/hermes")),
        clear_pending_reply=Mock(),
        output_critic=Mock(),
        sanitize=Mock(return_value="sanitized"),
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    relic_home = tmp_path / "relic"
    base = tmp_path / "hermes"
    ProfileRegistry(
        relic_home=relic_home, hermes_profiles_home=base / "profiles"
    ).initialize_user()
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    monkeypatch.setenv("HERMES_HOME", str(base / "profiles" / HERMES_USER_PROFILE_NAME))
    monkeypatch.delenv("HERMES_PROFILES_HOME", raising=False)
    monkeypatch.delenv("RELIC_RUNTIME_SCOPE_ID", raising=False)
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(tg, "_CONTROLLER", None)
    return SimpleNamespace(relic_home=relic_home, base=base)


def test_register_adds_gateway_hook_platform_handler_and_command(runtime):
    ctx = SimpleNamespace(
        register_hook=Mock(),
        register_platform_handler=Mock(),
        register_command=Mock(),
    )
    directory_plugin.register(ctx, dependencies_loader=_dependencies)

    hooks = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert hooks == BASE_HOOKS | {"pre_gateway_dispatch"}
    platform, factory = ctx.register_platform_handler.call_args.args
    assert platform == "telegram" and callable(factory)
    (name, handler), kwargs = ctx.register_command.call_args
    assert name == "gumi-setup" and callable(handler)
    assert kwargs["description"]
    controller = tg._CONTROLLER
    assert controller is not None
    assert controller.relic_home == runtime.relic_home
    assert controller.hermes_base_home == runtime.base
    assert controller.profiles_home == runtime.base / "profiles"


def test_register_does_not_raise_when_host_lacks_optional_apis(runtime, caplog):
    ctx = SimpleNamespace(register_hook=Mock())
    with caplog.at_level(logging.WARNING):
        directory_plugin.register(ctx, dependencies_loader=_dependencies)
    hooks = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert hooks == BASE_HOOKS | {"pre_gateway_dispatch"}
    assert "register_command" in caplog.text
    assert "register_platform_handler" in caplog.text


def test_register_survives_onboarding_failure(runtime, monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(tg, "register_onboarding", boom)
    ctx = SimpleNamespace(register_hook=Mock())
    with caplog.at_level(logging.WARNING):
        directory_plugin.register(ctx, dependencies_loader=_dependencies)
    hooks = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert hooks == BASE_HOOKS
    assert "onboarding" in caplog.text


def test_single_pre_llm_call_blocks_desktop_first_turn_only(runtime):
    from relic.hermes_plugin import desktop_onboarding as desk

    ctx = SimpleNamespace(
        register_hook=Mock(),
        register_platform_handler=Mock(),
        register_command=Mock(),
    )
    directory_plugin.register(ctx, dependencies_loader=_dependencies)

    pre_llm = [
        call.args[1]
        for call in ctx.register_hook.call_args_list
        if call.args[0] == "pre_llm_call"
    ]
    assert len(pre_llm) == 1  # one hook per name: the Desktop block is delegated
    hook = pre_llm[0]

    blocked = {"action": "block", "message": desk.BLOCK_MESSAGE}
    base = {"session_id": "s1", "user_message": "ciao", "turn_id": "t1"}
    assert hook(platform="desktop", is_first_turn=True, **base) == blocked
    # not a Desktop first turn: the normal path runs (no block payload)
    for extra in (
        {"platform": "desktop", "is_first_turn": False},
        {"platform": "telegram", "is_first_turn": True},
        {"platform": "cron", "is_first_turn": True},
    ):
        result = hook(**extra, **base)
        assert result != blocked


def test_installer_requires_the_gateway_hook():
    assert "pre_gateway_dispatch" in install.REQUIRED_HOOKS
    assert set(install.REQUIRED_HOOKS) == BASE_HOOKS | {"pre_gateway_dispatch"}


def test_owned_callbacks_includes_gateway_hook_without_invoking_it():
    owner = "hermes_plugins.relic_gumi"
    calls = []

    def make(name):
        def callback(**_kwargs):
            calls.append(name)

        callback.__module__ = owner
        return callback

    manager = SimpleNamespace(
        _hooks={name: [make(name)] for name in install.REQUIRED_HOOKS}
    )
    callbacks = install._owned_callbacks(manager, owner)
    assert set(callbacks) == set(install.REQUIRED_HOOKS)
    assert calls == []


def test_first_turn_veto_respects_the_foreground_filter(runtime):
    ctx = SimpleNamespace(
        register_hook=Mock(), register_platform_handler=Mock(), register_command=Mock()
    )
    directory_plugin.register(ctx, dependencies_loader=_dependencies)
    hook = next(
        call.args[1]
        for call in ctx.register_hook.call_args_list
        if call.args[0] == "pre_llm_call"
    )
    base = {"session_id": "s1", "user_message": "ciao", "turn_id": "t1",
            "platform": "desktop", "is_first_turn": True}
    assert hook(**base)["action"] == "block"  # the plain Desktop first turn
    # background / delegated turns are filtered out like everywhere else
    for extra in (
        {"execution_context": "background", "user_visible": True},
        {"execution_context": "foreground", "user_visible": False},
        {"parent_session_id": "parent-session"},
    ):
        result = hook(**{**base, **extra})
        assert not (isinstance(result, dict) and result.get("action") == "block"), extra
