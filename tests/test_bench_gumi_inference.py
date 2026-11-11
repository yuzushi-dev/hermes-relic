import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "bench_gumi_inference.py"
_SPEC = importlib.util.spec_from_file_location("bench_gumi_inference", _SCRIPT)
assert _SPEC and _SPEC.loader
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)


def test_request_overrides_unbounded_output_and_disables_streaming() -> None:
    request = bench.build_request(
        {"messages": [{"role": "user", "content": "secret"}], "max_tokens": 65536},
        max_tokens=128,
    )

    assert request["max_tokens"] == 128
    assert request["stream"] is False


def test_metrics_never_include_model_text() -> None:
    metrics = bench.metrics_from_response(
        {"usage": {"prompt_tokens": 12, "completion_tokens": 3}, "choices": [{"text": "secret"}]},
        elapsed_s=1.25,
    )

    assert metrics == {
        "elapsed_s": 1.25,
        "prompt_tokens": 12,
        "completion_tokens": 3,
    }
    assert "secret" not in str(metrics)
