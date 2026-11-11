#!/usr/bin/env python3
"""Run bounded, content-free inference benchmarks against llama-server."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def build_request(payload: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request = dict(payload)
    request["max_tokens"] = max_tokens
    request["stream"] = False
    return request


def metrics_from_response(response: dict[str, Any], *, elapsed_s: float) -> dict[str, Any]:
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    metrics: dict[str, Any] = {"elapsed_s": elapsed_s}
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            metrics[key] = value
    return metrics


def _json_request(url: str, *, payload: dict[str, Any] | None, timeout: float) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _slot_busy(slots: Any) -> bool | None:
    if not isinstance(slots, list):
        return None
    states = [item.get("is_processing") for item in slots if isinstance(item, dict)]
    return any(state is True for state in states) if states else False


def run_once(base_url: str, payload: dict[str, Any], *, max_tokens: int, timeout: float) -> dict[str, Any]:
    root = base_url.rstrip("/")
    slots_url = root.removesuffix("/v1") + "/slots"
    before = _json_request(slots_url, payload=None, timeout=timeout)
    started = time.monotonic()
    response = _json_request(
        root + "/chat/completions",
        payload=build_request(payload, max_tokens=max_tokens),
        timeout=timeout,
    )
    elapsed_s = round(time.monotonic() - started, 4)
    after = _json_request(slots_url, payload=None, timeout=timeout)
    metrics = metrics_from_response(response, elapsed_s=elapsed_s)
    metrics.update({"slot_busy_before": _slot_busy(before), "slot_busy_after": _slot_busy(after)})
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090/v1")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")
    if args.repeat <= 0:
        raise SystemExit("repeat must be positive")

    lines: list[str] = []
    for index in range(args.repeat):
        try:
            result = run_once(args.url, payload, max_tokens=args.max_tokens, timeout=args.timeout)
            result["iteration"] = index + 1
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            result = {"iteration": index + 1, "error": type(exc).__name__}
        lines.append(json.dumps(result, sort_keys=True))
    output = "\n".join(lines) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
