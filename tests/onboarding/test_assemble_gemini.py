"""Gemini key from the environment is validated like the CLI does."""

from __future__ import annotations

from onboarding.test_assemble_parity import CHAT_ID, TOKEN, _quick_answers
from relic.onboarding.assemble import assemble


def test_invalid_gemini_key_leaves_media_selected_but_disabled() -> None:
    assembled = assemble(
        _quick_answers(img="yes"),
        telegram_token=TOKEN,
        chat_id=CHAT_ID,
        gemini_key="not-a-gemini-key",
    )
    images = assembled.gumi_profile["media"]["images"]
    assert images["selected"] is True
    assert images["enabled"] is False
    assert images.get("remediation")
    assert "GEMINI_API_KEY" not in assembled.private_env


def test_valid_gemini_key_enables_media() -> None:
    key = "AIza" + "b" * 35
    assembled = assemble(
        _quick_answers(img="yes"), telegram_token=TOKEN, chat_id=CHAT_ID, gemini_key=key
    )
    assert assembled.gumi_profile["media"]["images"]["enabled"] is True
    assert "GEMINI_API_KEY" in assembled.private_env
