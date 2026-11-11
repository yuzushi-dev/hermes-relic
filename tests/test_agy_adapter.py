from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from relic.hermes_plugin.agy_adapter import (
    DEFAULT_AGY_MODEL,
    build_agy_command,
    build_prompt,
    invoke_agy,
    openai_response,
)


def test_build_prompt_preserves_roles_and_content() -> None:
    assert build_prompt(
        [
            {"role": "system", "content": "You are Gumi."},
            {"role": "user", "content": "Say hello."},
        ]
    ) == "[system]\nYou are Gumi.\n\n[user]\nSay hello."


def test_build_agy_command_uses_medium_flash_model_and_effort() -> None:
    assert build_agy_command(DEFAULT_AGY_MODEL, "medium") == [
        "agy",
        "--print",
        "--model",
        "gemini-3.6-flash-medium",
        "--effort",
        "medium",
    ]


def test_invoke_agy_passes_serialized_prompt_and_returns_text() -> None:
    calls: list[dict[str, object]] = []

    def runner(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout="  risposta agy  ", stderr="")

    result = invoke_agy(
        [{"role": "user", "content": "Ciao"}],
        runner=runner,
        timeout_seconds=12,
    )

    assert result == "risposta agy"
    assert calls == [
        {
            "command": [
                "agy",
                "--print",
                "--model",
                DEFAULT_AGY_MODEL,
                "--effort",
                "medium",
            ],
            "input": "[user]\nCiao",
            "text": True,
            "capture_output": True,
            "timeout": 12,
            "check": False,
        }
    ]


def test_invoke_agy_raises_on_cli_failure() -> None:
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="quota exhausted")

    with pytest.raises(RuntimeError, match="quota exhausted"):
        invoke_agy([{"role": "user", "content": "Ciao"}], runner=runner)


def test_openai_response_is_chat_completion_shape() -> None:
    payload = openai_response("gemini-3.6-flash-medium", "ciao")

    assert payload["object"] == "chat.completion"
    assert payload["model"] == "gemini-3.6-flash-medium"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "ciao"}
    json.dumps(payload)

def test_openai_stream_contains_delta_and_done_marker() -> None:
    from relic.hermes_plugin.agy_adapter import openai_stream

    chunks = list(openai_stream("gemini-3.6-flash-medium", "ciao"))

    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "ciao"}
    assert chunks[-1] == "[DONE]"