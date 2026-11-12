"""Provider error bodies must not leak echoed credentials into exceptions."""
from types import SimpleNamespace

import pytest

from relic.gumi_plugin import image_gen, lyria, tts


@pytest.mark.parametrize("provider", ["tts", "image", "lyria"])
def test_http_error_omits_body_and_echoed_key(monkeypatch, tmp_path, provider):
    import requests

    key = "test-fake-google-api-key"
    response = SimpleNamespace(
        status_code=403,
        text=f'{{"error":{{"message":"Rejected key: {key}"}}}}',
    )
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError) as exc:
        if provider == "tts":
            tts.synthesize("test speech", "Kore", tmp_path / "audio.ogg", key)
        elif provider == "image":
            image_gen._generate_image_via_gemini("test image", [], tmp_path / "image.png", key)
        else:
            lyria.LyriaGenerator(tmp_path, tmp_path, key)._call_lyria_gemini("test music")

    assert key not in str(exc.value)
    assert "Rejected key" not in str(exc.value)
    assert str(exc.value) == {
        "tts": "TTS synthesis failed: HTTP 403",
        "image": "Image generation failed: HTTP 403",
        "lyria": "Lyria API error: HTTP 403",
    }[provider]
