"""Local ACE-Step-1.5 music generation for Gumi checkins — drop-in
replacement for the Gemini/Lyria-based `LyriaGenerator._call_lyria()` in
lyria.py.

Talks to the standalone ACE-Step-1.5 REST API (github.com/ace-step/ACE-Step-1.5),
running locally (not ComfyUI — a separate uv-managed install, started with:

    ACESTEP_INIT_LLM=true ACESTEP_LM_MODEL_PATH=acestep-5Hz-lm-0.6B \\
    ACESTEP_LM_BACKEND=pt .venv/bin/python acestep/api_server.py \\
    --port 8001 --host 127.0.0.1

CPU-only, ~110s per 30s clip — validated 2026-09-18. Unlike the ComfyUI-hosted
attempt (ACE-Step via comfy_kitchen, and DiffRhythm), the upstream app has a
real maintained CPU path and just works.

Assigned onto LyriaGenerator as a bound method (see bottom of this file) so
`generate_and_deliver()`'s existing `self._call_lyria(lyric_prompt)` call
picks this up with zero changes elsewhere in lyria.py.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import os

import requests

ACESTEP_URL = os.environ.get("RELIC_ACESTEP_URL", "http://127.0.0.1:8001")
ACESTEP_LM_MODEL = "acestep-5Hz-lm-0.6B"
CLIP_DURATION_SECONDS = 30

# ~110s/clip observed; first request after a restart also pays a one-off
# model-load cost. Give plenty of headroom.
DEFAULT_TIMEOUT = 600


def _submit_job(prompt: str) -> str:
    """POST /release_task, return the task_id.

    `prompt` here is the single Gemini-written blob _build_lyric_prompt()
    produces (style description + embedded [Verse]/[Chorus] lines, meant for
    Lyria's single-text-in understanding). ACE-Step wants prompt (style) and
    lyrics as separate fields and does NOT auto-write lyrics into an empty
    field just because thinking=True (verified 2026-09-18: metas.lyrics stays
    "" with lyrics=""). Since we only have the one combined blob, and it
    already contains proper [Verse]/[Chorus] tags, send it as `lyrics` (where
    ACE-Step actually reads sung content from) as well as `prompt` (style
    guidance) — redundant but harmless, and doesn't require touching
    _build_lyric_prompt's output shape.
    """
    resp = requests.post(
        f"{ACESTEP_URL}/release_task",
        json={
            "prompt": prompt,
            "lyrics": prompt,
            "thinking": True,
            "lm_model_path": ACESTEP_LM_MODEL,
            "lm_backend": "pt",
            "vocal_language": "en",
            "inference_steps": 8,
            "audio_duration": CLIP_DURATION_SECONDS,
            "audio_format": "mp3",
            "use_random_seed": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        raise RuntimeError(f"ACE-Step job submission failed: {body}")
    return body["data"]["task_id"]


def _poll_job(task_id: str, timeout: int) -> dict:
    """POST /query_result until status leaves 0 (queued/running). Returns the
    first result entry's parsed metadata dict.

    Status codes (acestep/api/server_utils.py STATUS_MAP):
    0 = queued/running, 1 = succeeded, 2 = failed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = requests.post(
            f"{ACESTEP_URL}/query_result",
            json={"task_id_list": json.dumps([task_id])},
            timeout=30,
        )
        resp.raise_for_status()
        entry = resp.json()["data"][0]
        status = entry.get("status")
        if status == 1:
            results = json.loads(entry["result"])
            return results[0]
        if status == 2:
            raise RuntimeError(f"ACE-Step generation failed: {entry}")
        time.sleep(5)

    raise TimeoutError(f"ACE-Step generation did not finish within {timeout}s (task_id={task_id})")


def call_acestep_local(
    self,
    prompt: str,
    model: str = "",  # unused; kept for call-site compatibility with _call_lyria's signature
) -> tuple[Path, str]:
    """Generate via local ACE-Step-1.5, save to hermes_home/tmp/gumi-music/.

    Drop-in for LyriaGenerator._call_lyria(self, prompt, model=...): same
    signature, same (audio_path, lyrics_text) return shape.
    """
    task_id = _submit_job(prompt)
    result = _poll_job(task_id, DEFAULT_TIMEOUT)

    file_ref = result.get("file", "")
    if not file_ref:
        raise RuntimeError(f"No audio file in ACE-Step result: {result}")

    audio_resp = requests.get(f"{ACESTEP_URL}{file_ref}", timeout=60)
    audio_resp.raise_for_status()

    tmp_dir = self.hermes_home / "tmp" / "gumi-music"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = tmp_dir / f"music_{timestamp}.mp3"
    output_path.write_bytes(audio_resp.content)

    lyrics_text = result.get("metas", {}).get("lyrics", "") or prompt
    return output_path, lyrics_text
