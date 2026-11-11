"""Prose judge: transport contract, reasoning guard, and failure attribution.

The judge sits on the delivery path and fails open, so a broken judge and a
disabled judge used to write the identical log line (``gemma_score: null``).
These tests pin the distinction: every failure mode carries a status.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from relic.gumi_plugin import gemma_judge


def _payload(content: str, *, reasoning: str = "") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"finish_reason": "stop", "message": message}]}


def _transport(payload: dict | Exception, *, capture: dict | None = None):
    def _call(url: str, body: bytes, timeout: int) -> dict:
        if capture is not None:
            capture["url"] = url
            capture["body"] = json.loads(body.decode("utf-8"))
            capture["timeout"] = timeout
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _call


class TestScoring:
    def test_returns_score_and_ok_status(self):
        result = gemma_judge.judge("ciao", transport=_transport(_payload("90")))
        assert (result.score, result.status) == (90, "ok")

    def test_extracts_number_from_noisy_content(self):
        result = gemma_judge.judge("ciao", transport=_transport(_payload(" 85\n")))
        assert result.score == 85

    def test_clamps_out_of_range(self):
        assert gemma_judge.judge("x", transport=_transport(_payload("150"))).score == 100

    def test_empty_input_is_not_a_failure(self):
        result = gemma_judge.judge("   ", transport=_transport(_payload("90")))
        assert (result.score, result.status) == (None, "empty_input")


class TestFailureAttribution:
    def test_reasoning_leak_is_named(self):
        """A reasoning model that burns its budget must not read as 'disabled'."""
        result = gemma_judge.judge(
            "ciao", transport=_transport(_payload("", reasoning="Here's a thinking process"))
        )
        assert (result.score, result.status) == (None, "reasoning_not_disabled")

    def test_http_error_carries_its_code(self):
        err = urllib.error.HTTPError("u", 429, "too many", {}, None)  # type: ignore[arg-type]
        result = gemma_judge.judge("ciao", transport=_transport(err))
        assert (result.score, result.status) == (None, "http_429")

    def test_unreachable_backend(self):
        result = gemma_judge.judge("ciao", transport=_transport(urllib.error.URLError("down")))
        assert (result.score, result.status) == (None, "unreachable")

    def test_timeout(self):
        result = gemma_judge.judge("ciao", transport=_transport(TimeoutError()))
        assert (result.score, result.status) == (None, "timeout")

    def test_content_without_a_number(self):
        result = gemma_judge.judge("ciao", transport=_transport(_payload("non saprei")))
        assert (result.score, result.status) == (None, "no_number")

    def test_malformed_payload(self):
        result = gemma_judge.judge("ciao", transport=_transport({"nope": True}))
        assert (result.score, result.status) == (None, "bad_response")


class TestRequestContract:
    def test_disables_thinking_and_bounds_output(self):
        capture: dict = {}
        gemma_judge.judge("ciao", transport=_transport(_payload("70"), capture=capture))
        body = capture["body"]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["temperature"] == 0
        assert 0 < body["max_tokens"] <= 32
        assert capture["url"].endswith("/v1/chat/completions")

    def test_backend_is_configurable(self, monkeypatch):
        monkeypatch.setenv("RELIC_JUDGE_BASE_URL", "http://box:8090/")
        monkeypatch.setenv("RELIC_JUDGE_MODEL", "gemma4-e4b")
        capture: dict = {}
        gemma_judge.judge("ciao", transport=_transport(_payload("70"), capture=capture))
        assert capture["url"] == "http://box:8090/v1/chat/completions"
        assert capture["body"]["model"] == "gemma4-e4b"

    def test_no_lan_address_hardcoded_in_source(self):
        """The default must not ship a private address into the repo."""
        import inspect
        import re

        src = inspect.getsource(gemma_judge)
        assert not re.search(r"\b(?:10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", src)


class TestBackCompat:
    def test_judge_score_still_returns_a_bare_int(self):
        assert gemma_judge.judge_score("ciao", transport=_transport(_payload("64"))) == 64

    def test_judge_score_returns_none_on_failure(self):
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)  # type: ignore[arg-type]
        assert gemma_judge.judge_score("ciao", transport=_transport(err)) is None


class TestCalibrationLog:
    def test_status_is_recorded_next_to_the_score(self):
        from relic.gumi_plugin.prose_critic import ProseCritic, log_calibration_sample

        class _Sink:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def write(self, line: str) -> None:
                self.lines.append(line)

        sink = _Sink()
        verdict = ProseCritic().review("un messaggio qualunque")
        log_calibration_sample(
            verdict, "un messaggio qualunque",
            decision_type="checkin", gemma_score=None,
            gemma_status="http_429", sink=sink,
        )
        record = json.loads(sink.lines[0])
        assert record["gemma_score"] is None
        assert record["gemma_status"] == "http_429"


@pytest.mark.live
def test_live_local_backend_returns_a_score():
    """Opt-in: requires RELIC_JUDGE_BASE_URL pointing at a running backend."""
    import os

    if not os.environ.get("RELIC_JUDGE_BASE_URL"):
        pytest.skip("RELIC_JUDGE_BASE_URL not set")
    result = gemma_judge.judge(
        "Oggi ho rifatto la stessa strofa quattro volte e non mi convince ancora, "
        "tu come fai quando una cosa non viene?"
    )
    assert result.status == "ok", result
    assert 0 <= (result.score or -1) <= 100
