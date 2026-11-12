"""Google credentials must not occur in request URLs (including legacy paths)."""
from types import SimpleNamespace

import pytest
import requests

from relic.gumi_plugin import image_gen, lyria, tts


@pytest.mark.parametrize("operation", ["tts", "lyrics", "lyria_legacy", "models", "image_prompt", "image_legacy"])
def test_google_media_auth_uses_header_not_query(monkeypatch, tmp_path, operation):
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=400, text="test-error")
    monkeypatch.setattr(requests, "post", request)
    monkeypatch.setattr(requests, "get", request)
    key = "test-google-api-key"
    generator = lyria.LyriaGenerator(tmp_path, tmp_path / "user", key)
    actions = {
        "tts": lambda: tts.synthesize("test-text", "test-voice", tmp_path / "test.ogg", key),
        "lyrics": lambda: generator._build_lyric_prompt(),
        "lyria_legacy": lambda: generator._call_lyria_gemini("test-prompt"),
        "models": lambda: lyria.verify_lyria_models(key),
        "image_prompt": lambda: image_gen.generate_image_prompt_via_llm("test-avatar", {}, "test-mode", key),
        "image_legacy": lambda: image_gen._generate_image_via_gemini("test-prompt", [], tmp_path / "test.png", key),
    }
    try:
        actions[operation]()
    except RuntimeError:
        pass  # Status handling remains covered by media tests; inspect transport here.
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert key not in url and "?key=" not in url
    assert kwargs.get("headers", {}).get("x-goog-api-key") == key
    assert kwargs["timeout"] > 0
