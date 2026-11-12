"""Checkin media dispatcher, routes LLM output to appropriate media generators.

Handles text, voice, image, and music delivery after gate decision.

Stdout contract (User-facing):
  - text branch: sanitized message text only
  - voice/image/music: empty, delivery happens via Telegram Bot API directly
  - ALL [WARN], [DRY-RUN], MEDIA: lines go to stderr only

Hermes reads stdout and may forward it to the User chat; nothing
operator-facing must ever appear there.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from relic.gumi_plugin.media_state import record_media_delivery, record_outbound_delivery, record_sent_media_memory
from relic.gumi_plugin.image_gen import generate_checkin_image
from relic.gumi_plugin.tts import synthesize_checkin_audio
from relic.gumi_plugin.lyria import LyriaGenerator
from relic.gumi_plugin.output_sanitizer import sanitize_for_user, strip_terminal_full_stops
from relic.hermes_plugin.lifecycle import require_outbound_ready

_TRAILING_SYMBOL_RE = re.compile(
    r"(\s*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?|\ufe0f)+\s*)$"
)

# Gate control tokens / bracketed headers that may prefix a message blob but are
# not User-facing prose. Used to look past them when detecting questions.
_GATE_CONTROL_RE = re.compile(r"^(DELIVER|BLOCKED|NO_REPLY|SILENT)\b", re.IGNORECASE)


def _strip_gate_control_lines(text: str) -> str:
    """Drop leading gate-control/header lines so question detection sees the prose."""
    kept = [
        ln for ln in text.splitlines()
        if not _GATE_CONTROL_RE.match(ln.strip())
        and not (ln.strip().startswith("[") and ln.strip().endswith("]"))
    ]
    return "\n".join(kept).strip()


def ensure_checkin_question_mark(text: str) -> str:
    """Ensure check-in questions keep an explicit question mark.

    Some models generate a semantically interrogative sentence and end with an
    emoji instead of punctuation. Put the question mark before trailing emoji so
    voice/text/image branches all preserve the question contract.
    """
    stripped = text.rstrip()
    if not stripped or "?" in stripped:
        return text
    match = _TRAILING_SYMBOL_RE.search(stripped)
    if match:
        body = stripped[: match.start()].rstrip()
        return f"{body}?{match.group(1)}"
    return f"{stripped}?"


# Italian interrogative openers used to detect when a proactive/diegetic line is
# actually a question (so we only restore a dropped "?" on real questions and
# never turn a statement into one). One- and two-token openers.
_IT_INTERROGATIVE_OPENERS = frozenset(
    {
        "come", "cosa", "che", "chi", "dove", "quando", "perché", "perche",
        "quanto", "quanta", "quanti", "quante", "quale", "quali",
        "preferisci", "hai", "sei", "vuoi", "riesci", "pensi", "senti",
        "ti va", "ti funziona", "ti andrebbe", "ti capita", "secondo te",
        "che cosa", "com'è", "come va", "come stai",
    }
)


def _looks_interrogative(text: str) -> bool:
    """Heuristic: does this line read as an Italian question?

    True if it already carries a "?" anywhere, or it opens with a known
    interrogative word/phrase. Conservative on purpose, used to decide whether
    to restore a dropped question mark without inventing questions.
    """
    body = _TRAILING_SYMBOL_RE.sub("", _strip_gate_control_lines(text)).strip().lower()
    if not body:
        return False
    if "?" in body:
        return True
    tokens = body.split()
    if not tokens:
        return False
    first = tokens[0].strip(".,;:!")
    two = " ".join(tokens[:2]).strip(".,;:!")
    return first in _IT_INTERROGATIVE_OPENERS or two in _IT_INTERROGATIVE_OPENERS


def ensure_question_mark_if_interrogative(text: str) -> str:
    """Restore a dropped question mark only when the line reads as a question.

    Used for proactive re-engagement, whose questions are optional, unlike
    check-ins, where the question is mandatory and ``ensure_checkin_question_mark``
    applies unconditionally.
    """
    if not _looks_interrogative(text):
        return text
    return ensure_checkin_question_mark(text)


def clean_image_caption(caption: str) -> str:
    """Remove only trailing periods from image captions.

    The old cleanup removed all terminal punctuation, including ``?``. The
    intended style rule is only "no final full stop" on captions.
    """
    return strip_terminal_full_stops(caption.strip())


def parse_gate_output(llm_output: str) -> dict:
    """Parse gate output to extract media type and content.

    Returns dict with keys:
        tipo: "text"|"voice"|"image"|"music"
        testo: full content (for text/voice/music)
        caption: extracted caption line (for image)
        image_prompt: extracted image_prompt line (for image)
        argomento: topic tag for the per-topic pause (never delivered)
    """
    lines = llm_output.strip().split("\n")
    media_type = "text"
    content_lines = []
    caption = ""
    image_prompt = ""
    argomento = ""

    for line in lines:
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("tipo:"):
            media_type = stripped.split(":", 1)[1].strip().lower()
        elif low.startswith("ora:"):
            pass
        elif low.startswith("caption:"):
            caption = stripped.split(":", 1)[1].strip()
        elif low.startswith("image_prompt:"):
            image_prompt = stripped.split(":", 1)[1].strip()
        elif low.startswith("argomento:"):
            argomento = stripped.split(":", 1)[1].strip()
        else:
            content_lines.append(stripped)

    testo = "\n".join(l for l in content_lines if l).strip()

    return {
        "tipo": media_type,
        "testo": testo,
        "caption": caption,
        "image_prompt": image_prompt,
        "argomento": argomento,
    }


def _get_api_key() -> Optional[str]:
    """Expose the key only after an explicit opt-in to Google media processing.

    Hermes may already have a Google key for unrelated tools. That alone is
    not permission to send companion content to Google. The media pipelines
    share this gate, including local image/music backends with Google prompt
    generation fallbacks.
    """
    if os.environ.get("RELIC_ENABLE_GOOGLE_MEDIA") != "1":
        return None
    from relic.secrets import secret_value

    return secret_value("GEMINI_API_KEY") or None


_DELIVERY_ENV_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_ALLOWED_USERS")


def _load_env(hermes_home: Path) -> dict[str, str]:
    """Delivery settings from the active Hermes scope; never parse credential files."""
    from relic.secrets import secret_value

    return {name: value for name in _DELIVERY_ENV_NAMES if (value := secret_value(name))}


def _load_canonical_user_profile(relic_user_home: Path):
    from relic.profile.registry import UserProfile

    try:
        profile = json.loads((relic_user_home / "user_profile.json").read_text(encoding="utf-8"))
        return UserProfile.from_artifact(profile, user_home=relic_user_home)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("canonical User profile is unavailable") from exc

def _canonical_outbound_delivery_allowed(tipo: str, hermes_home: Path) -> bool:
    """Require canonical User delivery and per-media settings when configured."""
    try:
        from relic.paths import get_relic_home
        from relic.user_runtime import media_delivery_armed

        allowed = media_delivery_armed(
            tipo,
            relic_home=get_relic_home(),
            hermes_home=hermes_home,
        )
        return True if allowed is None else allowed
    except Exception:
        return False


def _canonical_delivery_block(tipo: str, hermes_home: Path) -> dict | None:
    if _canonical_outbound_delivery_allowed(tipo, hermes_home):
        return None
    print("[dispatch] blocked: canonical User delivery is not armed", file=sys.stderr)
    return {"tipo": tipo, "success": False, "reason": "canonical_delivery_not_armed"}


def _send_telegram_media(
    hermes_home: Path,
    media_path: Path,
    media_type: str,
    caption: str = "",
    title: str = "",
    performer: str = "",
) -> bool:
    """Send a media file to Telegram via Bot API.

    media_type: 'voice' | 'image' | 'music'
    - voice: no caption, no text, pure voice note
    - image: caption shown below photo (stripped of trailing punctuation)
    - music: title + performer set on audio player, no caption
    Returns True on success.
    """
    import urllib.request

    env = _load_env(hermes_home)
    bot_token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_ALLOWED_USERS")

    if not bot_token or not chat_id:
        print("[WARN] Missing TELEGRAM_BOT_TOKEN or chat_id, skipping Telegram delivery", file=sys.stderr)
        return False

    if not media_path.exists():
        print(f"[WARN] Media file not found: {media_path}", file=sys.stderr)
        return False

    _METHOD = {"voice": "sendVoice", "image": "sendPhoto", "music": "sendAudio"}
    _FIELD  = {"voice": "voice",     "image": "photo",     "music": "audio"}
    method = _METHOD.get(media_type, "sendDocument")
    field  = _FIELD.get(media_type, "document")

    url = f"https://api.telegram.org/bot{bot_token}/{method}"

    boundary = "ReLiCboundary123"
    body_parts: list[bytes] = []

    def _part(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body_parts.append(_part("chat_id", str(chat_id)))

    # voice: no caption, no extra text
    if media_type == "image" and caption:
        caption = sanitize_for_user(caption)
        if caption is None:
            return False
        clean_caption = clean_image_caption(caption)
        if clean_caption:
            body_parts.append(_part("caption", clean_caption[:1024]))

    if media_type == "music":
        # title from lyrics (first meaningful line), no caption
        if title:
            title = sanitize_for_user(title)
            if title is None:
                return False
            body_parts.append(_part("title", title[:64]))
        if performer:
            performer = sanitize_for_user(performer)
            if performer is None:
                return False
            body_parts.append(_part("performer", performer[:64]))

    with open(media_path, "rb") as fh:
        file_data = fh.read()

    filename = media_path.name
    body_parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + file_data + b"\r\n"
    )
    body_parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(body_parts)
    if not _canonical_outbound_delivery_allowed(media_type, hermes_home):
        print("[dispatch] blocked: canonical User delivery is not armed", file=sys.stderr)
        return False


    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return True
            print(f"[WARN] Telegram API error: {result.get('description')}", file=sys.stderr)
            return False
    except Exception as exc:
        print(f"[WARN] Telegram delivery failed: {exc}", file=sys.stderr)
        return False


def _send_telegram_text(hermes_home: Path, text: str) -> bool:
    """Send a plain text message to Telegram via Bot API sendMessage.

    Returns True on success. Silently returns False if token/chat_id missing
    or on any network error.
    """
    import urllib.parse
    import urllib.request

    env = _load_env(hermes_home)
    bot_token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_ALLOWED_USERS")

    if not bot_token or not chat_id:
        return False
    if not text:
        return False
    text = sanitize_for_user(text)
    if text is None:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text[:4096]}).encode()
    if not _canonical_outbound_delivery_allowed("text", hermes_home):
        print("[dispatch] blocked: canonical User delivery is not armed", file=sys.stderr)
        return False

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return True
            print(f"[WARN] Telegram sendMessage error: {result.get('description')}", file=sys.stderr)
            return False
    except Exception as exc:
        print(f"[WARN] Telegram text delivery failed: {exc}", file=sys.stderr)
        return False




def _music_title_from_lyrics(caption: str) -> str:
    """Extract a short song title from the first lyric line."""
    import re
    # Strip timestamp prefix like "[0.0:2.9] " if present
    lines = [re.sub(r"^\[\d+\.\d+[:\d.]*\]\s*", "", l).strip() for l in caption.splitlines() if l.strip()]
    first = lines[0] if lines else ""
    # Capitalise, max 40 chars, no trailing punctuation
    title = re.sub(r"[.!?;:]+$", "", first).strip()
    return title[:40] if title else "Canzone di Gumi"


def _critic_block_reason(text: str) -> Optional[str]:
    """Run the delivery-time OutputCritic on User-facing text.

    Returns a block reason when the manuscript's final language guardrail must
    suppress delivery (dependency/need claims, false physical experience,
    unauthorized disclosure pressure), else None. Fail-open: any critic error
    allows delivery so the runtime never blocks on the guardrail itself.
    """
    try:
        from relic.gumi_plugin.critic import OutputCritic

        verdict = OutputCritic().review(text or "")
        return None if verdict.allow else verdict.reason
    except Exception:
        return None


def _prose_block_reason(text: str, decision_type: str = "") -> Optional[str]:
    """Delivery-time prose-quality scorer (AI-tell detection).

    Observe-only by default: logs score + violations to stderr and returns None
    (never blocks). Hard block is opt-in via RELIC_PROSE_HARD_BLOCK=1, gated by
    a threshold not yet calibrated on real Italian Gumi output. Fail-open: any
    error allows delivery so the runtime never blocks on the scorer itself.
    """
    try:
        from relic.gumi_plugin.prose_critic import ProseCritic, log_calibration_sample

        def _truthy(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}

        hard = _truthy("RELIC_PROSE_HARD_BLOCK")
        verdict = ProseCritic(hard_block=hard).review(text or "")

        # Gemma LLM judge (opt-in): higher recall than the regex on Italian slop.
        # Synchronous + timeout-bounded; fine on this non-interactive cron path.
        gemma_score: int | None = None
        gemma_status = "disabled"
        gemma_block = False
        if _truthy("RELIC_PROSE_GEMMA_JUDGE"):
            from relic.gumi_plugin.gemma_judge import judge

            result = judge(text or "")
            gemma_score, gemma_status = result.score, result.status
            if gemma_score is not None:
                try:
                    cut = int(os.environ.get("RELIC_PROSE_GEMMA_CUT", "50"))
                except ValueError:
                    cut = 50
                gemma_block = hard and gemma_score < cut

        # Numeric-only calibration log (no text/hash) for threshold tuning.
        log_calibration_sample(
            verdict, text or "", decision_type=decision_type,
            gemma_score=gemma_score, gemma_status=gemma_status,
        )
        if verdict.violations or gemma_status != "disabled":
            print(
                f"[dispatch] prose_critic score={verdict.score} "
                f"reason={verdict.reason} violations={','.join(verdict.violations)} "
                f"gemma={gemma_score} gemma_status={gemma_status}",
                file=sys.stderr,
            )
        if gemma_block:
            return f"prose:gemma_low_{gemma_score}"
        return None if verdict.allow else f"prose:{verdict.reason}"
    except Exception:
        return None


def _event_kind_for_continuity(continuity_kind: str) -> str:
    if continuity_kind == "proactivity":
        return "proactive"
    return continuity_kind or "checkin"


def _record_delivered_runtime_event(runtime_scope_id: str, continuity_kind: str) -> None:
    """Record a successful outbound delivery through canonical Chronicle."""
    try:
        from relic.chronicle.emitter import emit_event
        from relic.chronicle.enums import EventCategory

        emit_event(
            event_type="outbound_delivered",
            event_category=EventCategory.MESSAGE,
            source_module="relic.gumi_plugin.checkin_media_dispatcher",
            runtime_scope_id=runtime_scope_id,
            payload={
                "channel": "telegram",
                "continuity_kind": _event_kind_for_continuity(continuity_kind),
                "delivery_status": "delivered",
            },
            tags=["channel:outbound", "channel:telegram"],
        )
    except Exception as exc:
        print(f"[WARN] outbound Chronicle event write failed: {exc}", file=sys.stderr)


def _pause_delivered_topic(
    relic_user_home: Path, topic: str, continuity_kind: str
) -> None:
    """Start the random per-topic pause once a check-in/proactive is delivered."""
    if continuity_kind not in {"checkin", "proactivity"} or not topic:
        return
    from relic.checkin.topic_pause import record_topic_pause

    record_topic_pause(Path(relic_user_home) / "checkin", topic)


def dispatch(
    llm_output: str,
    hermes_home: Path,
    relic_user_home: Path,
    dry_run: bool = False,
    force: bool = False,
    continuity_kind: str = "checkin",
) -> dict:
    """Dispatch media based on gate output.

    Args:
        llm_output: Raw LLM output from checkin_message
        hermes_home: Path to Hermes profile home
        relic_user_home: Path to canonical User root
        continuity_kind: Continuity lane for outbound delivery
        dry_run: If True, simulate without API calls

    Returns:
        {"tipo": str, "success": bool, "output": str}

    Stdout contract: only sanitized User-facing text is printed.
    Voice/image/music deliver via Telegram Bot API, stdout stays empty.
    """
    parsed = parse_gate_output(llm_output)
    tipo = parsed["tipo"]
    # The image contract has a caption and prompt, but no separate text body.
    source_text = parsed["testo"] or (parsed["caption"] if tipo == "image" else "")
    testo = sanitize_for_user(source_text)
    if testo is None:
        print("[dispatch] source text blocked by sanitizer, silent drop", file=sys.stderr)
        return {"tipo": tipo, "success": False, "reason": "sanitized_empty"}
    for field in ("caption", "image_prompt"):
        if parsed[field]:
            parsed[field] = sanitize_for_user(parsed[field])
            if parsed[field] is None:
                print(f"[dispatch] {field} blocked by sanitizer, silent drop", file=sys.stderr)
                return {"tipo": tipo, "success": False, "reason": "sanitized_empty"}
    if not dry_run:
        try:
            require_outbound_ready(hermes_home)
        except RuntimeError as exc:
            print(f"[dispatch] blocked: {exc}", file=sys.stderr)
            return {"tipo": tipo, "success": False, "reason": "lifecycle_not_ready", "retry_after": 90}
        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked
    try:
        runtime_scope_id = _load_canonical_user_profile(relic_user_home).runtime_scope_id
    except ValueError:
        print("[dispatch] blocked: canonical User profile is unavailable", file=sys.stderr)
        return {"tipo": tipo, "success": False, "reason": "invalid_user_profile"}
    if tipo in {"text", "voice", "image"}:
        if continuity_kind == "checkin":
            # Check-ins always carry a question: enforce unconditionally.
            testo = ensure_checkin_question_mark(testo)
            if parsed.get("caption"):
                parsed["caption"] = ensure_checkin_question_mark(parsed["caption"])
        elif continuity_kind in {"proactivity", "diegetic"}:
            # Proactive/diegetic questions are optional: only restore a dropped
            # "?" when the line actually reads as a question, never invent one.
            testo = ensure_question_mark_if_interrogative(testo)
            if parsed.get("caption"):
                parsed["caption"] = ensure_question_mark_if_interrogative(parsed["caption"])

    # Delivery-time language guardrail: applies to every User-facing branch
    # (text body, and the source text synthesized into voice/image/music) before
    # anything is sent or printed.
    block_reason = _critic_block_reason(testo)
    if block_reason:
        print(
            f"[dispatch] blocked by output critic: {block_reason}, silent drop",
            file=sys.stderr,
        )
        return {"tipo": tipo, "success": False, "reason": f"critic_blocked:{block_reason}"}

    # Delivery-time prose-quality scorer (observe-only unless RELIC_PROSE_HARD_BLOCK).
    prose_reason = _prose_block_reason(testo, decision_type=continuity_kind)
    if prose_reason:
        print(
            f"[dispatch] blocked by prose critic: {prose_reason}, silent drop",
            file=sys.stderr,
        )
        return {"tipo": tipo, "success": False, "reason": f"prose_blocked:{prose_reason}"}

    api_key = _get_api_key()

    if tipo == "text":
        safe = testo
        if dry_run:
            print("[DRY-RUN] Text send skipped", file=sys.stderr)
            return {"tipo": "text", "success": True, "dry_run": True, "output": safe}
        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked
        print(f"[dispatch] text delivery chars={len(safe)}", file=sys.stderr)
        delivered = _send_telegram_text(hermes_home, safe)
        if delivered:
            record_outbound_delivery(hermes_home, "telegram", "text")
            _record_delivered_runtime_event(runtime_scope_id, continuity_kind)
            _pause_delivered_topic(relic_user_home, parsed["argomento"], continuity_kind)
        return {"tipo": "text", "success": True, "output": safe, "telegram_delivered": delivered}

    elif tipo == "voice":
        if dry_run:
            print("[DRY-RUN] Voice synthesis skipped", file=sys.stderr)
            return {"tipo": "voice", "success": True, "dry_run": True}

        if not api_key:
            print("[WARN] No GEMINI_API_KEY, skipping voice", file=sys.stderr)
            return {"tipo": "voice", "success": False, "reason": "no_api_key"}

        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked

        audio_path = synthesize_checkin_audio(testo, hermes_home, relic_user_home, api_key)
        record_media_delivery(hermes_home, "voice")
        delivered = _send_telegram_media(hermes_home, audio_path, "voice")
        if delivered:
            record_outbound_delivery(hermes_home, "telegram", "voice")
            _record_delivered_runtime_event(runtime_scope_id, continuity_kind)
            _pause_delivered_topic(relic_user_home, parsed["argomento"], continuity_kind)
            _summary = testo.strip().replace("\n", " ")[:90]
            record_sent_media_memory(hermes_home, f"Ho mandato una nota vocale: «{_summary}»")
        status = "DELIVERED" if delivered else "LOCAL_ONLY"
        print(f"MEDIA:{audio_path} [{status}]", file=sys.stderr)
        # No stdout: voice delivered directly via Telegram Bot API
        return {"tipo": "voice", "success": True, "audio_path": str(audio_path), "telegram_delivered": delivered}

    elif tipo == "image":
        if dry_run:
            print("[DRY-RUN] Image generation skipped", file=sys.stderr)
            return {"tipo": "image", "success": True, "dry_run": True}

        if not api_key:
            print("[WARN] No GEMINI_API_KEY, skipping image", file=sys.stderr)
            return {"tipo": "image", "success": False, "reason": "no_api_key"}

        caption = parsed["caption"] or testo
        image_prompt = parsed["image_prompt"]

        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked

        if image_prompt:
            # Gumi wrote the image prompt: use it directly
            from relic.gumi_plugin.image_gen import generate_image, collect_reference_images
            from datetime import date
            import hashlib
            ref_images = collect_reference_images(relic_user_home, "close_selfie")
            today = date.today()
            seed = int(hashlib.sha256(f"{runtime_scope_id}{today}".encode()).hexdigest(), 16) % 10000
            tmp_dir = hermes_home / "tmp" / "gumi-images"
            output_path = tmp_dir / f"checkin_{today.isoformat()}_{seed:04d}.jpg"
            image_path = generate_image(image_prompt, ref_images, output_path, api_key)
        else:
            # Fallback: no image_prompt from Gumi, generate via checkin pipeline
            from datetime import datetime as _dt
            hour = _dt.now().hour
            time_of_day = "morning" if 6 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening" if 18 <= hour < 22 else "night"
            image_path = generate_checkin_image(
                hermes_home,
                relic_user_home,
                api_key,
                time_of_day,
                runtime_scope_id,
                testo,
            )

        record_media_delivery(hermes_home, "image")
        delivered = _send_telegram_media(hermes_home, image_path, "image", caption=caption[:1024])
        if delivered:
            record_outbound_delivery(hermes_home, "telegram", "image")
            _record_delivered_runtime_event(runtime_scope_id, continuity_kind)
            _pause_delivered_topic(relic_user_home, parsed["argomento"], continuity_kind)
            _cap = (caption or "").strip().replace("\n", " ")[:90]
            record_sent_media_memory(
                hermes_home,
                f"Ho mandato una mia foto: «{_cap}»" if _cap else "Ho mandato una mia foto",
            )
        status = "DELIVERED" if delivered else "LOCAL_ONLY"
        print(f"MEDIA:{image_path} [{status}]", file=sys.stderr)
        # No stdout: image + caption delivered directly via Telegram Bot API
        return {"tipo": "image", "success": True, "image_path": str(image_path), "caption": caption, "telegram_delivered": delivered}

    elif tipo == "music":
        if dry_run:
            print("[DRY-RUN] Music generation skipped", file=sys.stderr)
            return {"tipo": "music", "success": True, "dry_run": True}

        if not api_key:
            print("[WARN] No GEMINI_API_KEY, skipping music", file=sys.stderr)
            return {"tipo": "music", "success": False, "reason": "no_api_key"}

        lyria_prompt = testo
        if not lyria_prompt:
            return {"tipo": "music", "success": False, "reason": "empty_lyria_prompt"}

        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked

        generator = LyriaGenerator(hermes_home, relic_user_home, api_key)
        result = generator.generate_and_deliver(
            target=runtime_scope_id,
            lyria_prompt=lyria_prompt,
            dry_run=False,
            force=force,
        )

        if not result.get("success"):
            print("[WARN] Music generation failed", file=sys.stderr)
            return {"tipo": "music", "success": False, "reason": result.get("reason")}

        record_media_delivery(hermes_home, "music")
        caption = result.get("caption", "")
        if caption:
            caption = sanitize_for_user(caption)
            if caption is None:
                print("[dispatch] music caption blocked by sanitizer, silent drop", file=sys.stderr)
                return {"tipo": "music", "success": False, "reason": "sanitized_empty"}
        media_file = result.get("audio_path", "")
        music_title = _music_title_from_lyrics(caption)
        performer = sanitize_for_user("Gumi")
        if performer is None:
            return {"tipo": "music", "success": False, "reason": "sanitized_empty"}
        delivered = _send_telegram_media(
            hermes_home, Path(media_file), "music",
            title=music_title, performer=performer,
        ) if media_file else False
        if delivered:
            record_outbound_delivery(hermes_home, "telegram", "music")
            _record_delivered_runtime_event(runtime_scope_id, continuity_kind)
            _pause_delivered_topic(relic_user_home, parsed["argomento"], continuity_kind)
            _title = (music_title or "").strip()[:80]
            record_sent_media_memory(
                hermes_home,
                f"Ho condiviso un brano che ho buttato giù («{_title}»)" if _title
                else "Ho condiviso un brano che ho buttato giù",
            )
        status = "DELIVERED" if delivered else "LOCAL_ONLY"
        print(f"MEDIA:{media_file} [{status}] title_chars={len(music_title)}", file=sys.stderr)
        # No stdout: music delivered directly via Telegram Bot API
        return {"tipo": "music", "success": True, "audio_path": media_file, "caption": caption, "telegram_delivered": delivered}

    else:
        safe = testo
        if dry_run:
            print("[DRY-RUN] Fallback text send skipped", file=sys.stderr)
            return {"tipo": "text", "success": True, "dry_run": True, "output": safe}
        blocked = _canonical_delivery_block(tipo, hermes_home)
        if blocked:
            return blocked
        print(f"[dispatch] text delivery chars={len(safe)}", file=sys.stderr)
        delivered = _send_telegram_text(hermes_home, safe)
        if delivered:
            record_outbound_delivery(hermes_home, "telegram", "text")
            _record_delivered_runtime_event(runtime_scope_id, continuity_kind)
            _pause_delivered_topic(relic_user_home, parsed["argomento"], continuity_kind)
        return {"tipo": "text", "success": True, "output": safe, "telegram_delivered": delivered}


def main():
    """CLI entry point for checkin media dispatcher."""
    import argparse

    parser = argparse.ArgumentParser(description="Checkin media dispatcher")
    parser.add_argument("--llm-output", required=True, help="LLM output from checkin_message")
    parser.add_argument("--hermes-home", type=Path, help="Hermes profile home path")
    parser.add_argument("--user-home", type=Path, help="Canonical User root")
    parser.add_argument(
        "--continuity-kind",
        choices=("checkin", "proactivity", "diegetic"),
        default="checkin",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate without API calls")
    parser.add_argument("--force", action="store_true", help="Bypass cooldowns (for testing)")

    args = parser.parse_args()
    relic_home = Path(os.environ.get("RELIC_HOME", Path.home() / ".relic"))
    relic_user_home = args.user_home or Path(
        os.environ.get("RELIC_USER_HOME", relic_home / "user")
    )
    try:
        user = _load_canonical_user_profile(relic_user_home)
    except ValueError as exc:
        parser.error(str(exc))
    configured_scope = os.environ.get("RELIC_RUNTIME_SCOPE_ID")
    if configured_scope and configured_scope != user.runtime_scope_id:
        parser.error("RELIC_RUNTIME_SCOPE_ID does not match the canonical User profile")
    hermes_home = args.hermes_home or Path(
        os.environ.get(
            "HERMES_HOME",
            Path.home() / ".hermes" / "profiles" / user.hermes_profile_name,
        )
    )

    force = args.force or os.environ.get("RELIC_FORCE_MEDIA_TYPE", "") != ""
    dispatch(
        llm_output=args.llm_output,
        hermes_home=hermes_home,
        relic_user_home=relic_user_home,
        dry_run=args.dry_run,
        force=force,
        continuity_kind=args.continuity_kind,
    )


if __name__ == "__main__":
    main()
