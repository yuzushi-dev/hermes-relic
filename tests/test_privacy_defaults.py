"""Network selection must be local unless the operator explicitly opts in."""
from __future__ import annotations

import importlib
import json
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


def test_clean_environment_selects_only_loopback_classifiers(monkeypatch):
    from relic.checkin import attribution_jury as jury, facet_updater
    from relic.gumi_plugin import gemma_judge

    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def urlopen(request, **kwargs):
        seen.append((request.full_url, json.loads(request.data)["model"]))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    try:
        with patch.dict("os.environ", {}, clear=True):
            importlib.reload(jury)
            assert jury.CLOUD_ENDPOINT == ""
            for model, template in jury.JUDGES:
                jury.llm_choose(model, "synthetic reply", "", [], template)
            # Also check legacy cloud-family requests cannot silently pick a host.
            for model in ("gpt-oss:120b", "minimax-m3"):
                assert urlparse(jury._endpoint_for_model(model)).hostname == "localhost"
            facet_updater._call_llm_extract("f", "Facet", "Description", "low", "high", "Q", "A")
            assert urlparse(gemma_judge._base_url()).hostname == "localhost"
            assert gemma_judge._model() == "gemma4-e4b"
            assert len(seen) == len(jury.JUDGES) + 1
            assert all(urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"} for url, _ in seen)
            assert all(not model.endswith("-cloud") for _, model in seen)
    finally:
        importlib.reload(jury)


@pytest.mark.parametrize("enabled", [None, "0", "false", "1"])
def test_ambient_google_key_is_not_media_opt_in(monkeypatch, enabled):
    from relic.gumi_plugin.checkin_media_dispatcher import _get_api_key

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    monkeypatch.delenv("RELIC_ENABLE_GOOGLE_MEDIA", raising=False)
    if enabled is not None:
        monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", enabled)
    assert _get_api_key() == ("synthetic-key" if enabled == "1" else None)
