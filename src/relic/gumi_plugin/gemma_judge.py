"""Gemma prose-quality judge for the delivery path.

The deterministic ProseCritic regex has ~33% recall on diverse Italian slop
(measured via gemma calibration). This module adds an LLM judge that scores how
human/natural a message reads (0-100), for the non-interactive cron delivery
path where a few seconds of latency is acceptable.

Constraints:
- Synchronous but timeout-bounded; fail-open (score None) on any error/timeout
  so it never blocks delivery on the judge itself.
- In-process on live text only, nothing is persisted (privacy gate still owns
  the delivered text). The judge returns a number, never stores prose.
- Opt-in: callers gate on env (see checkin_media_dispatcher).

Every failure carries a ``status``. Fail-open used to make a broken judge and a
disabled judge write the identical calibration line, which is how a judge that
never once produced a number went unnoticed for weeks.

Backend: OpenAI-compatible ``/v1/chat/completions``, served by llama.cpp and by
Ollama alike. Configure with ``RELIC_JUDGE_BASE_URL`` and ``RELIC_JUDGE_MODEL``.
Reasoning models must be told not to think: with a 16-token budget the chain of
thought eats the whole allowance and the reply comes back empty.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4-e4b"
MAX_TOKENS = 16

# Shared with scripts/prose_calibration.py: keep the rubric identical so the
# runtime judge and the offline calibration speak the same scale.
JUDGE_PROMPT = (
    "Valuta quanto questo messaggio suona naturale e umano (NON generato da AI). "
    "Rispondi SOLO con un numero intero da 0 a 100, dove 100 = voce umana "
    "perfettamente naturale e 0 = chiaramente testo AI pieno di cliché.\n\n"
    "Messaggio:\n{msg}\n\nNumero:"
)

Transport = Callable[[str, bytes, int], "dict[str, Any]"]


@dataclass(frozen=True)
class JudgeResult:
    """A score, or the reason there isn't one."""

    score: int | None
    status: str


def _base_url() -> str:
    return (os.environ.get("RELIC_JUDGE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("RELIC_JUDGE_MODEL") or DEFAULT_MODEL


def _http_transport(url: str, body: bytes, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def judge(text: str, *, timeout: int = 20, transport: Transport | None = None) -> JudgeResult:
    """Score how natural ``text`` reads, or say why the judge could not.

    No retries: this sits on the delivery path, so a single bounded attempt then
    fail-open. Higher score = more natural; lower = more AI slop.
    """
    if not isinstance(text, str) or not text.strip():
        return JudgeResult(None, "empty_input")

    body = json.dumps(
        {
            "model": _model(),
            "messages": [{"role": "user", "content": JUDGE_PROMPT.format(msg=text)}],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            # Reasoning models otherwise spend the whole budget thinking and
            # return an empty content field.
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode("utf-8")

    call = transport or _http_transport
    try:
        payload = call(f"{_base_url()}/v1/chat/completions", body, timeout)
    except urllib.error.HTTPError as exc:
        return JudgeResult(None, f"http_{exc.code}")
    except urllib.error.URLError:
        return JudgeResult(None, "unreachable")
    except TimeoutError:
        return JudgeResult(None, "timeout")
    except (ValueError, OSError):
        return JudgeResult(None, "bad_response")

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return JudgeResult(None, "bad_response")

    content = str(message.get("content") or "")
    if not content.strip():
        if str(message.get("reasoning_content") or "").strip():
            return JudgeResult(None, "reasoning_not_disabled")
        return JudgeResult(None, "empty_response")

    match = re.search(r"\d{1,3}", content)
    if not match:
        return JudgeResult(None, "no_number")
    return JudgeResult(max(0, min(100, int(match.group()))), "ok")


def judge_score(
    text: str, *, timeout: int = 20, transport: Transport | None = None
) -> int | None:
    """Bare score for callers that do not care why it is missing."""
    return judge(text, timeout=timeout, transport=transport).score
