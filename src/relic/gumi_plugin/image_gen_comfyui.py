"""Local ComfyUI image generation for Gumi checkins — drop-in replacement for
the Gemini-based `generate_image()` in image_gen.py.

Same signature as the original (plus one new optional kwarg), same return
type, so `checkin_media_dispatcher.py` and `generate_checkin_image()` do not
need to change at all — only the `from relic.gumi_plugin.image_gen import
generate_image` import line would need to point here instead.

Talks to ComfyUI's plain HTTP API (POST /prompt, GET /history, GET /view,
POST /upload/image) — no MCP, no extra dependency beyond `requests`, which
image_gen.py already uses.

Workflow graph and defaults (checkpoint, IPAdapter preset/weight, sampler)
match the manual run validated on 2026-09-17 against a local ComfyUI instance.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

import os

import requests

# --- Config -----------------------------------------------------------
# TODO: move to relic-gumi's own config/env layer once agreed.
COMFYUI_URL = os.environ.get("RELIC_COMFYUI_URL", "http://127.0.0.1:8188")
COMFYUI_CHECKPOINT = "epiCRealism.safetensors"
IPADAPTER_PRESET = "PLUS (high strength)"
IPADAPTER_WEIGHT = 0.8

# Character LoRA (rank 32/alpha 16, epiCRealism base).
# Validated to reproduce identity from the trigger token alone; combined here
# with IP-Adapter for even stronger, more consistent identity conditioning.
# Trigger token is injected into every prompt automatically (see generate_image).
LORA_NAME = "gumichar_v1.safetensors"
LORA_TRIGGER = "gumichar"
LORA_STRENGTH_MODEL = 0.7
LORA_STRENGTH_CLIP = 0.7

# 4x-UltraSharp: ~64MB, ~23s on CPU for a 512x512 image (validated 2026-09-18).
# Cheap enough to leave on by default.
UPSCALE_MODEL = "4x-UltraSharp.pth"

# ControlNet (OpenPose) — validated 2026-09-18. Pre-extracted skeleton PNGs
# (via OpenposePreprocessor, run once offline against the matching reference
# photo in Visual_Identity/) ship alongside this module in pose_refs/, named
# by visual mode. Only modes with a pre-extracted skeleton get pose guidance;
# any other mode falls back to the original (no-ControlNet) behaviour.
CONTROLNET_MODEL = "control_v11p_sd15_openpose_fp16.safetensors"
CONTROLNET_STRENGTH = 1.0
POSE_REFS_DIR = Path(__file__).parent / "pose_refs"
POSE_REFS_BY_MODE = {
    "close_selfie": "pose_close_selfie.png",
    "mirror_corner_selfie": "pose_mirror_corner_selfie.png",
    "bed_soft_frame": "pose_bed_soft_frame.png",
    "desk_process_shot": "pose_desk_process_shot.png",
}


def _pose_skeleton_path(mode: Optional[str]) -> Optional[Path]:
    """Resolve a pre-extracted pose skeleton for `mode`, if one exists."""
    if not mode:
        return None
    filename = POSE_REFS_BY_MODE.get(mode)
    if not filename:
        return None
    path = POSE_REFS_DIR / filename
    return path if path.exists() else None

DEFAULT_NEGATIVE_PROMPT = (
    "plastic skin, over-smoothed AI face, glamour shot, editorial fashion model, "
    "exaggerated grin, cartoon, painting, illustration, cgi, deformed, blurry, watermark"
)

# CPU inference on this host: ~2min/image once the checkpoint is warm, plus a
# one-off checkpoint+IPAdapter load on the first request after a ComfyUI
# restart (can add another 30-60s). Gemini's default was 90s; give this much
# more room so a cold instance doesn't get cut off mid-render.
DEFAULT_TIMEOUT = 300


def _upload_reference_image(comfyui_url: str, image_path: Path) -> str:
    """Upload one reference image to ComfyUI's input/ dir, return its filename."""
    with open(image_path, "rb") as f:
        resp = requests.post(
            f"{comfyui_url}/upload/image",
            files={"image": (image_path.name, f, "image/png")},
            data={"overwrite": "true"},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()["name"]


def _build_workflow(
    prompt: str,
    negative_prompt: str,
    reference_filenames: list[str],
    checkpoint: str,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    seed: int,
    upscale: bool,
    pose_filename: Optional[str] = None,
) -> dict:
    """API-format graph: checkpoint -> IPAdapterAdvanced(batched ref images,
    averaged embeds) -> KSampler -> save.

    One LoadImage per reference, chained through core ImageBatch nodes into a
    single IMAGE batch tensor, fed to IPAdapterAdvanced with
    combine_embeds="average" — this is the multi-reference equivalent of what
    the Gemini path did by stuffing up to 6 images into one multimodal call.
    """
    graph: dict = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["1", 0],
                "clip": ["1", 1],
                "lora_name": LORA_NAME,
                "strength_model": LORA_STRENGTH_MODEL,
                "strength_clip": LORA_STRENGTH_CLIP,
            },
        },
        "3": {
            "class_type": "IPAdapterUnifiedLoader",
            "inputs": {"model": ["2", 0], "preset": IPADAPTER_PRESET},
        },
    }

    # combine_embeds="concat" (not "average") — validated 2026-09-17: average
    # dilutes the signature green hair toward a generic dark tone since it's
    # a minority/noisy trait across the 6 references' varied lighting; concat
    # preserves it while still gaining the richer environment continuity that
    # motivated using 6 references over 1.
    #
    # One LoadImage per reference: node ids "img0", "img1", ...
    for i, fname in enumerate(reference_filenames):
        graph[f"img{i}"] = {
            "class_type": "LoadImage",
            "inputs": {"image": fname},
        }

    # Chain ImageBatch to fold all references into one batched IMAGE tensor.
    # batch0 = img0 (no-op passthrough for a single reference); otherwise
    # fold img1..imgN-1 into a running batch via core ImageBatch (2 inputs).
    if len(reference_filenames) == 1:
        batched_image_ref = ["img0", 0]
    else:
        running = ["img0", 0]
        for i in range(1, len(reference_filenames)):
            node_id = f"batch{i}"
            graph[node_id] = {
                "class_type": "ImageBatch",
                "inputs": {"image1": running, "image2": [f"img{i}", 0]},
            }
            running = [node_id, 0]
        batched_image_ref = running

    graph["4"] = {
        "class_type": "IPAdapterAdvanced",
        "inputs": {
            "model": ["3", 0],
            "ipadapter": ["3", 1],
            "image": batched_image_ref,
            "weight": IPADAPTER_WEIGHT,
            "weight_type": "linear",
            "combine_embeds": "concat",
            "start_at": 0.0,
            "end_at": 1.0,
            "embeds_scaling": "V only",
        },
    }

    graph.update({
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["2", 1]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["2", 1]},
        },
        "7": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
    })

    # ControlNet (OpenPose): when a pre-extracted skeleton is available for
    # this mode, route the positive/negative conditioning through it before
    # KSampler; otherwise KSampler conditions on the plain CLIPTextEncode
    # outputs (identical to pre-ControlNet behaviour).
    positive_cond = ["5", 0]
    negative_cond = ["6", 0]
    if pose_filename:
        graph["pose_img"] = {
            "class_type": "LoadImage",
            "inputs": {"image": pose_filename},
        }
        graph["cn_loader"] = {
            "class_type": "ControlNetLoader",
            "inputs": {"control_net_name": CONTROLNET_MODEL},
        }
        graph["cn_apply"] = {
            "class_type": "ControlNetApplyAdvanced",
            "inputs": {
                "positive": positive_cond,
                "negative": negative_cond,
                "control_net": ["cn_loader", 0],
                "image": ["pose_img", 0],
                "strength": CONTROLNET_STRENGTH,
                "start_percent": 0.0,
                "end_percent": 1.0,
            },
        }
        positive_cond = ["cn_apply", 0]
        negative_cond = ["cn_apply", 1]

    graph.update({
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": positive_cond,
                "negative": negative_cond,
                "latent_image": ["7", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["8", 0], "vae": ["1", 2]},
        },
    })

    final_image_ref = ["9", 0]
    if upscale:
        graph["11"] = {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": UPSCALE_MODEL},
        }
        graph["12"] = {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["11", 0], "image": ["9", 0]},
        }
        final_image_ref = ["12", 0]

    graph["10"] = {
        "class_type": "SaveImage",
        "inputs": {"images": final_image_ref, "filename_prefix": "gumi_checkin"},
    }
    return graph


def _submit_and_wait(comfyui_url: str, workflow: dict, timeout: int) -> dict:
    """POST the workflow, poll /history until it's done, return the history entry."""
    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{comfyui_url}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json()["prompt_id"]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hist_resp = requests.get(f"{comfyui_url}/history/{prompt_id}", timeout=15)
        hist_resp.raise_for_status()
        history = hist_resp.json()
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            if status.get("completed") or status.get("status_str") == "error":
                return entry
        time.sleep(2)

    raise TimeoutError(
        f"ComfyUI generation did not finish within {timeout}s (prompt_id={prompt_id})"
    )


def generate_image(
    prompt: str,
    reference_images: list[Path],
    output_path: Path,
    api_key: Optional[str] = None,  # unused; kept for call-site compatibility
    model: str = COMFYUI_CHECKPOINT,  # repurposed: checkpoint filename
    timeout: int = DEFAULT_TIMEOUT,
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT,
    comfyui_url: str = COMFYUI_URL,
    width: int = 512,
    height: int = 512,
    steps: int = 25,
    cfg: float = 6.0,
    upscale: bool = True,
    mode: Optional[str] = None,
) -> Path:
    """Generate via local ComfyUI (IP-Adapter), save to output_path.

    Drop-in for the Gemini `generate_image()`: same first four positional
    args, same return type. `api_key` is accepted and ignored so existing
    call sites (`generate_image(image_prompt, ref_images, output_path,
    api_key)`) keep working unmodified.

    `mode` is optional and additive: when it names a visual mode with a
    pre-extracted pose skeleton (see POSE_REFS_BY_MODE), generation is
    additionally conditioned via ControlNet/OpenPose on that pose. Callers
    that don't pass `mode` (or pass one without a skeleton) get the original
    IP-Adapter-only behaviour, unchanged.
    """
    if not reference_images:
        raise ValueError("generate_image needs at least one reference image for IP-Adapter")

    # Matches Gemini's up-to-6 multi-image behaviour: upload every reference
    # and average their IPAdapter embeddings (see combine_embeds="average"
    # in _build_workflow) instead of conditioning on a single anchor.
    reference_filenames = [
        _upload_reference_image(comfyui_url, img_path) for img_path in reference_images
    ]

    pose_path = _pose_skeleton_path(mode)
    pose_filename = _upload_reference_image(comfyui_url, pose_path) if pose_path else None

    # Prepend the LoRA trigger token so the character LoRA activates on every
    # request without callers needing to know it exists.
    lora_prompt = f"{LORA_TRIGGER}, {prompt}"

    seed = int.from_bytes(str(output_path).encode(), "little") % (2**32)
    workflow = _build_workflow(
        prompt=lora_prompt,
        negative_prompt=negative_prompt,
        reference_filenames=reference_filenames,
        checkpoint=model,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
        seed=seed,
        upscale=upscale,
        pose_filename=pose_filename,
    )

    entry = _submit_and_wait(comfyui_url, workflow, timeout)
    status = entry.get("status", {})
    if status.get("status_str") == "error":
        raise RuntimeError(f"ComfyUI generation failed: {status}")

    outputs = entry.get("outputs", {})
    image_info = None
    for node_output in outputs.values():
        if "images" in node_output:
            image_info = node_output["images"][0]
            break
    if image_info is None:
        raise RuntimeError("No image in ComfyUI history outputs")

    view_resp = requests.get(
        f"{comfyui_url}/view",
        params={
            "filename": image_info["filename"],
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        },
        timeout=30,
    )
    view_resp.raise_for_status()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(view_resp.content)
    return output_path
