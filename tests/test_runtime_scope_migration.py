from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock



_SCOPE_A = "a" * 64


def test_directory_hooks_invoke_user_runtime_seams_without_subject_environment(monkeypatch):
    from relic.hermes_plugin import directory_plugin

    observed_environment: list[tuple[str | None, str | None]] = []
    provider = Mock()
    hermes_home = Path("/hermes")


    def prefetch(_message: str) -> str:
        observed_environment.append(
            (os.environ.get("RELIC_RUNTIME_SCOPE_ID"), os.environ.get("RELIC_SUBJECT_ID"))
        )
        return "continuity"

    provider.prefetch.side_effect = prefetch
    provider.sync_turn.return_value = None
    dependencies = SimpleNamespace(
        memory_provider=Mock(return_value=provider),
        inject_context=Mock(return_value={"context": "context"}),
        pending_ack_instruction=Mock(return_value="ack"),
        resolve_hermes_home=Mock(return_value=hermes_home),
        clear_pending_reply=Mock(),
        output_critic=Mock(return_value=SimpleNamespace(review=Mock(return_value=SimpleNamespace(allow=True)))),
        sanitize=Mock(return_value="safe"),
    )
    ctx = SimpleNamespace(register_hook=Mock())
    monkeypatch.delenv("RELIC_SUBJECT_ID", raising=False)

    directory_plugin._register_hooks(
        ctx,
        runtime_scope_id=_SCOPE_A,
        profile_name=HERMES_USER_PROFILE_NAME,
        relic_home=None,
        dependencies=dependencies,
    )
    hooks = {call.args[0]: call.args[1] for call in ctx.register_hook.call_args_list}

    result = hooks["pre_llm_call"](
        execution_context="foreground", user_visible=True, user_message="hello"
    )
    hooks["post_llm_call"](
        execution_context="foreground",
        user_visible=True,
        user_message="hello",
        assistant_response="welcome",
    )
    transformed = hooks["transform_llm_output"](
        execution_context="foreground", user_visible=True, response_text="safe"
    )

    assert result == {"context": "continuity\n\ncontext\n\nack"}
    assert transformed is None
    dependencies.memory_provider.assert_called_once_with(
        runtime_scope_id=_SCOPE_A, hermes_profile_id=HERMES_USER_PROFILE_NAME, relic_home=None
    )
    dependencies.pending_ack_instruction.assert_called_once_with(hermes_home)
    assert observed_environment == [(_SCOPE_A, None)]
    assert "RELIC_SUBJECT_ID" not in os.environ


