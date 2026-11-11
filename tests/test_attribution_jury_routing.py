from __future__ import annotations

import json

from relic.checkin import attribution_jury


def _fake_response():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"best": "NONE"}'}}]}
            ).encode()

    return Response()


def test_jury_routes_cloud_families_and_keeps_local_judge_local(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization")))
        return _fake_response()

    monkeypatch.setattr(attribution_jury.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(attribution_jury, "_next_key", lambda: "test-secret")
    candidates = [{"id": "facet-a", "name": "A", "description": "D"}]

    assert attribution_jury.llm_choose(
        "gemma4-e4b", "reply", "", candidates, 0
    ) == "NONE"
    assert attribution_jury.llm_choose(
        "gpt-oss:120b", "reply", "", candidates, 1
    ) == "NONE"
    assert attribution_jury.llm_choose(
        "minimax-m3", "reply", "", candidates, 0
    ) == "NONE"

    assert seen[0] == (attribution_jury.LOCAL_ENDPOINT + "/chat/completions", None)
    assert seen[1] == (
        attribution_jury.CLOUD_ENDPOINT + "/chat/completions",
        "Bearer test-secret",
    )
    assert seen[2] == (
        attribution_jury.CLOUD_ENDPOINT + "/chat/completions",
        "Bearer test-secret",
    )

