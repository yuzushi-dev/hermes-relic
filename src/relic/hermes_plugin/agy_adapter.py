"""Small OpenAI-compatible HTTP adapter for the Antigravity ``agy`` CLI."""

from __future__ import annotations

import json
import os
import subprocess
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence

DEFAULT_AGY_MODEL = "gemini-3.6-flash-medium"
DEFAULT_AGY_EFFORT = "medium"
DEFAULT_AGY_TIMEOUT_SECONDS = 300


def build_prompt(messages: Sequence[Mapping[str, Any]]) -> str:
    """Render OpenAI chat messages into the text prompt accepted by ``agy``."""
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text"
            )
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def build_agy_command(model: str = DEFAULT_AGY_MODEL, effort: str = DEFAULT_AGY_EFFORT) -> list[str]:
    return ["agy", "--print", "--model", model, "--effort", effort]


def invoke_agy(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str = DEFAULT_AGY_MODEL,
    effort: str = DEFAULT_AGY_EFFORT,
    timeout_seconds: int = DEFAULT_AGY_TIMEOUT_SECONDS,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    result = runner(
        build_agy_command(model, effort),
        input=build_prompt(messages),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "agy failed").strip()
        raise RuntimeError(detail)
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError("agy returned an empty response")
    return text


def openai_response(model: str, text: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-agy-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }

def openai_stream(model: str, text: str) -> list[dict[str, Any] | str]:
    return [
        {
            "id": f"chatcmpl-agy-{int(time.time() * 1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        },
        "[DONE]",
    ]


def _send_sse(handler: BaseHTTPRequestHandler, chunks: Sequence[dict[str, Any] | str]) -> None:
    body = "".join(
        f"data: {json.dumps(chunk) if chunk != '[DONE]' else chunk}\n\n"
        for chunk in chunks
    ).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _request_model() -> str:
    return os.environ.get("AGY_MODEL", DEFAULT_AGY_MODEL)


def _request_effort() -> str:
    return os.environ.get("AGY_EFFORT", DEFAULT_AGY_EFFORT)


def _request_timeout() -> int:
    return int(os.environ.get("AGY_TIMEOUT_SECONDS", str(DEFAULT_AGY_TIMEOUT_SECONDS)))


def _run_request(request: Mapping[str, Any]) -> str:
    return invoke_agy(
        request.get("messages", []),
        model=_request_model(),
        effort=_request_effort(),
        timeout_seconds=_request_timeout(),
    )



class _Handler(BaseHTTPRequestHandler):
    server_version = "relic-agy-adapter/1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            text = _run_request(request)
            model = _request_model()
            if request.get("stream"):
                _send_sse(self, openai_stream(model, text))
            else:
                self._send_json(HTTPStatus.OK, openai_response(model, text))
        except Exception as exc:  # pragma: no cover - exercised through the process boundary
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": {"message": str(exc)}})

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def main() -> None:
    host = os.environ.get("AGY_ADAPTER_HOST", "127.0.0.1")
    port = int(os.environ.get("AGY_ADAPTER_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
