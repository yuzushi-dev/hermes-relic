"""Canonical Hermes directory-plugin bridge for RELIC Gumi."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

logger = logging.getLogger(__name__)


PLUGIN_KEY = "relic-gumi"
MAINTENANCE_TOOLSET = "gumi-maintenance"
SAFE_OUTPUT_REPLACEMENT = "I'm here with you. What would feel most helpful right now?"
_RELIC_ENV_LOCK = threading.RLock()

_CANON_FILES = frozenset(
    {
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    }
)
_REPORT_FILES = {
    "state_compaction": "cron/world_state_compaction_report.json",
    "candidate_review": "cron/continuity_candidate_review_report.json",
}


@dataclass(frozen=True)
class _RelicDependencies:
    memory_provider: Any
    inject_context: Any
    pending_ack_instruction: Any
    resolve_hermes_home: Any
    clear_pending_reply: Any
    output_critic: Any
    sanitize: Any


def _load_relic_dependencies() -> _RelicDependencies:
    from relic.gumi_plugin.critic import OutputCritic
    from relic.gumi_plugin.output_sanitizer import sanitize_for_user
    from relic.hermes_plugin.context_injection import inject_context
    from relic.hermes_plugin.memory_provider import RelicMemoryProvider
    from relic.hermes_plugin.pending_reply import (
        clear_pending_reply,
        pending_ack_instruction,
        resolve_hermes_home,
    )

    return _RelicDependencies(
        memory_provider=RelicMemoryProvider,
        inject_context=inject_context,
        pending_ack_instruction=pending_ack_instruction,
        resolve_hermes_home=resolve_hermes_home,
        clear_pending_reply=clear_pending_reply,
        output_critic=OutputCritic,
        sanitize=sanitize_for_user,
    )


def _text(value: Any) -> str:
    return "" if value is None else value if isinstance(value, str) else str(value)


def _discard_unconfirmed_checkin(
    relic_home: str | None, started_at: datetime | None = None
) -> None:
    if relic_home is None:
        return
    try:
        from relic.checkin.context_builder import discard_latest_checkin_attempt

        discard_latest_checkin_attempt(
            Path(relic_home) / "user" / "checkin" / "relic.db",
            not_before=(started_at or datetime.now(timezone.utc)) - timedelta(minutes=10),
        )
    except Exception:
        pass


def _dispatch_and_record(
    message_text: str,
    kind: str | None,
    hermes_home: Path,
    relic_home: str,
    runtime_scope_id: str,
    started_at: datetime | None = None,
) -> str | None:
    """Deliver one cron message and record its outcome; returns dry-run JSON."""
    try:
        from relic.gumi_plugin.checkin_media_dispatcher import dispatch

        result = dispatch(
            message_text,
            hermes_home,
            Path(relic_home) / "user",
            continuity_kind=kind or "checkin",
            **({"dry_run": True} if os.environ.get("_RELIC_MANUAL_DRYRUN") == "1" else {}),
        )
        if os.environ.get("_RELIC_MANUAL_DRYRUN") == "1":
            return json.dumps({
                "dryrun": True, "kind": kind, "response": message_text,
                "dispatch": result,
            }, ensure_ascii=False)
        if bool(result.get("telegram_delivered")):
            from relic.profile.user_runtime import record_delivery_outcome

            record_delivery_outcome(
                hermes_home,
                kind,
                relic_home=Path(relic_home),
                runtime_scope_id=runtime_scope_id,
            )
        elif kind == "checkin":
            _discard_unconfirmed_checkin(relic_home, started_at)
    except Exception:
        if kind == "checkin":
            _discard_unconfirmed_checkin(relic_home, started_at)
    return None


_DISPATCH_PAYLOAD_LIMIT = 256 * 1024
_DISPATCH_WRITE_TIMEOUT = 1.0


def _write_dispatch_payload(pipe, payload: bytes) -> None:
    """Bound private stdin handoff, including a child that never reads.

    This budget starts after Popen returns; it does not bound process creation
    or the detached delivery itself. Unbuffered nonblocking writes keep close()
    from flushing a blocked userspace buffer after the deadline.
    """
    import selectors
    import time

    deadline = time.monotonic() + _DISPATCH_WRITE_TIMEOUT
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    pending = memoryview(payload)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("dispatch handoff timed out")
            try:
                count = os.write(fd, pending)
            except BlockingIOError:
                continue
            if count <= 0:
                raise OSError("dispatch pipe closed")
            pending = pending[count:]


def _spawn_dispatch(
    message_text: str,
    kind: str | None,
    hermes_home: Path,
    relic_home: str,
    runtime_scope_id: str,
) -> bool:
    """Return whether the private job was handed off to a detached worker.

    Delivery can outlast the host's 30s transform_llm_output timeout and the
    cron worker's lifetime: local media generation (ACE-Step, ComfyUI) takes
    minutes, and even a text message runs critic and prose checks against the
    local model. A timed-out hook is skipped and the message is lost.
    """
    import subprocess
    import sys

    job = {
        "message_text": message_text,
        "kind": kind,
        "hermes_home": str(hermes_home),
        "relic_home": relic_home,
        "runtime_scope_id": runtime_scope_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    log_path = Path(hermes_home) / "logs" / "cron-dispatch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The entry point's sys.path fallback is process-local. Carry the active
    # runtime into detached delivery too, including when it came from _vendor.
    from relic.secrets import scoped_credentials

    payload = json.dumps({"job": job, "credentials": scoped_credentials()},
                         ensure_ascii=False).encode("utf-8")
    if len(payload) > _DISPATCH_PAYLOAD_LIMIT:
        logger.warning("relic dispatch rejected: oversized payload (%d bytes)", len(payload))
        return False
    # Inherit only process essentials and delivery settings read by the worker:
    # dispatcher prose/media gates, local renderer endpoints and Gemma judge.
    # HERMES_PROFILES_HOME is used by registry/outcome recording; no provider
    # credentials or arbitrary HERMES_*/RELIC_* settings belong in the child env.
    # Proxy URLs can contain the user's network credentials; deliberately carry
    # them because delivery fails behind a proxy without them. No other
    # credentials are inherited.
    allowed = {
        "PATH", "HOME", "LANG", "TMPDIR", "HERMES_PROFILES_HOME",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "RELIC_ENABLE_GOOGLE_MEDIA", "RELIC_COMFYUI_URL", "RELIC_ACESTEP_URL",
        "RELIC_PROSE_HARD_BLOCK", "RELIC_PROSE_GEMMA_JUDGE", "RELIC_PROSE_GEMMA_CUT",
        "RELIC_JUDGE_BASE_URL", "RELIC_JUDGE_MODEL",
    }
    environment = {
        key: value for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_")
    }
    environment.update(
        HERMES_HOME=str(hermes_home), RELIC_HOME=relic_home,
        RELIC_RUNTIME_SCOPE_ID=runtime_scope_id,
    )
    runtime_parent = str(Path(__file__).resolve().parents[2])
    # Use the active import path, not an ambient PYTHONPATH that may be stale.
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        part for part in (runtime_parent, *sys.path) if part
    ))
    process = subprocess.Popen(
        [sys.executable, "-m", "relic.hermes_plugin.directory_plugin"],
        stdin=subprocess.PIPE,
        bufsize=0,
        # Media providers can print prompts/errors: never route their streams
        # into persistent logs. Only the metadata receipt below is logged.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=environment,
    )
    assert process.stdin is not None
    handed_off = False
    try:
        _write_dispatch_payload(process.stdin, payload)
        handed_off = True
    except (OSError, TimeoutError):
        process.kill()
        process.wait(timeout=1)
    finally:
        process.stdin.close()
    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"dispatch pid={process.pid} bytes={len(payload)} "
                      f"handoff={'queued' if handed_off else 'failed'}\n")
    except OSError:
        # A receipt failure must not invalidate a job already handed off.
        pass
    return handed_off


def _dispatch_scoped_job(job: dict, credentials: dict) -> None:
    """Restore only the parent's scoped snapshot in a detached worker."""
    try:
        from agent.secret_scope import (
            set_secret_scope, reset_secret_scope,
            set_multiplex_context, reset_multiplex_context,
        )
    except ImportError:
        return  # Older hosts cannot safely run detached credentialed delivery.
    from relic.secrets import SECRET_NAMES

    values = {name: value for name, value in credentials.items()
              if name in SECRET_NAMES and isinstance(value, str)}
    scope = set_secret_scope(values, profile_home=str(job["hermes_home"]))
    multiplex = set_multiplex_context(True)
    try:
        _dispatch_and_record(**job)
    finally:
        reset_secret_scope(scope)
        reset_multiplex_context(multiplex)


def _load_user_runtime(relic_home: str | None) -> tuple[str, str]:
    from relic.profile.registry import ProfileRegistry

    registry = ProfileRegistry(relic_home=Path(relic_home) if relic_home else None)
    user = registry.load_user()
    if user is None:
        raise RuntimeError(
            f"relic-gumi requires canonical User profile at {registry.user_profile_path}"
        )
    return user.runtime_scope_id, user.hermes_profile_name


_DIEGETIC_HOOK_EMOJI_TRAILER_RE = re.compile(
    r"[\s\U0001F300-\U0001FAFF☀-➿]*$"
)
_DIEGETIC_HOOK_QUESTION_STARTERS = (
    "chi ",
    "cosa ",
    "che cosa",
    "come ",
    "quando ",
    "dove ",
    "perché ",
    "perche ",
    "quale ",
    "quali ",
    "quanto ",
    "quanta ",
    "quanti ",
    "quante ",
    "tu che",
    "tu come",
    "tu cosa",
    "tu quando",
    "tu dove",
)


def _ensure_diegetic_question_mark(text: str) -> str:
    """Append '?' to a diegetic hook message that reads as a question but
    is missing the punctuation the prompt contract requires, inserting it
    before any trailing emoji rather than after."""
    if not text or "?" in text:
        return text
    trailer_match = _DIEGETIC_HOOK_EMOJI_TRAILER_RE.search(text)
    trailer = trailer_match.group(0) if trailer_match else ""
    body = text[: len(text) - len(trailer)] if trailer else text
    if not body:
        return text
    last_sentence = re.split(r"[.!\n]", body)[-1].strip()
    if last_sentence.lower().startswith(_DIEGETIC_HOOK_QUESTION_STARTERS):
        return f"{body}?{trailer}"
    return text


# Platforms Hermes assigns to agents that never talk to the subject directly.
_INTERNAL_PLATFORMS = frozenset({"cron", "subagent", "curator", "gateway_hygiene"})
_ADMITTED_TURNS_MAX = 512


def _explicit_turn_metadata(kwargs: dict[str, Any]) -> bool:
    return any(
        key in kwargs for key in ("execution_context", "user_visible", "subject_visible")
    )


def _foreground_user_turn(kwargs: dict[str, Any]) -> bool:
    """Strict check on metadata supplied by the host (patched or future Hermes)."""
    visibility = (
        kwargs["subject_visible"]
        if "subject_visible" in kwargs
        else kwargs.get("user_visible")
    )
    return kwargs.get("execution_context") == "foreground" and visibility is True


def _derived_user_turn(kwargs: dict[str, Any]) -> bool:
    """Stock-Hermes check: only signals every hook already receives.

    Cron, delegated sub-agents, the curator and gateway hygiene have their own
    ``platform``. Sub-agents and detached forks also carry a ``parent_session_id``,
    which a normal turn never has (resume, branch and compression do not set it).
    """
    if "platform" not in kwargs or not _text(kwargs.get("turn_id")).strip():
        return False  # not a real Hermes hook call: fail closed
    platform = _text(kwargs.get("platform")).strip().lower()
    if platform in _INTERNAL_PLATFORMS:
        return False
    return not _text(kwargs.get("parent_session_id")).strip()


class _AdmittedTurns:
    """Bounded record of turn ids that passed the gate in ``pre_llm_call``.

    Hermes skips ``pre_llm_call`` and ``post_llm_call`` for detached forks (the
    background memory review) but still calls ``transform_llm_output`` with the
    parent's session id. A turn that was never admitted up front is therefore not
    ours to touch.
    """

    def __init__(self, limit: int = _ADMITTED_TURNS_MAX) -> None:
        self._limit = limit
        self._ids: dict[str, None] = {}
        self._lock = threading.Lock()

    def admit(self, turn_id: str) -> None:
        if not turn_id:
            return
        with self._lock:
            self._ids.pop(turn_id, None)
            self._ids[turn_id] = None
            while len(self._ids) > self._limit:
                self._ids.pop(next(iter(self._ids)))

    def __contains__(self, turn_id: str) -> bool:
        with self._lock:
            return bool(turn_id) and turn_id in self._ids


def _admit_user_turn(kwargs: dict[str, Any], admitted: _AdmittedTurns) -> bool:
    """Gate for ``pre_llm_call``; records the turn id on success."""
    if _explicit_turn_metadata(kwargs):
        return _foreground_user_turn(kwargs)
    if not _derived_user_turn(kwargs):
        return False
    admitted.admit(_text(kwargs.get("turn_id")))
    return True


def _foreground_candidate(kwargs: dict[str, Any]) -> bool:
    """Same foreground filter as ``_admit_user_turn``, without admitting the turn."""
    if _explicit_turn_metadata(kwargs):
        return _foreground_user_turn(kwargs)
    return _derived_user_turn(kwargs)


def _is_admitted_user_turn(kwargs: dict[str, Any], admitted: _AdmittedTurns) -> bool:
    """Gate for the hooks that run after ``pre_llm_call``."""
    if _explicit_turn_metadata(kwargs):
        return _foreground_user_turn(kwargs)
    return _derived_user_turn(kwargs) and _text(kwargs.get("turn_id")) in admitted


def _cron_job_id(session_id: str) -> str | None:
    """Job id of the running cron execution, or None.

    Hermes exposes the current fire through ``cron.execution_identity``. Its
    context-local ``CronExecution`` has a ``job_id`` but no session id. The
    scheduler binds/resets it per execution, including across worker threads;
    the session-id pattern is the fallback when that API is unavailable.
    """
    try:
        from cron.execution_identity import current_cron_execution

        execution = current_cron_execution()
        if execution is not None and isinstance(execution.job_id, str) and execution.job_id:
            return execution.job_id
    except Exception:
        pass
    match = re.fullmatch(r"cron_(.+)_([0-9]{8})_([0-9]{6})", session_id)
    return match.group(1) if match else None


def _first_message_output(
    dependencies: Any,
    hermes_home: Path | None,
    relic_home: str | None,
    session_id: str,
    response_text: str,
) -> str:
    """Let ONLY the owned first-message cron job deliver, then mark it sent.

    Every other cron turn stays ``[SILENT]``. The job is identified by its exact id
    in the profile's ``cron/jobs.json`` (never by prefix or prompt text), and the
    canonical first-message gate must still be open.
    """
    silent = "[SILENT]"
    if hermes_home is None:
        return silent
    if relic_home is None:
        from relic.paths import get_relic_home

        relic_home = os.environ.get("RELIC_HOME") or str(get_relic_home())
    stripped = response_text.strip()
    if not stripped or stripped.upper().startswith(silent):
        return silent
    job_id = _cron_job_id(session_id)
    if job_id is None:
        return silent
    try:
        from relic.user_runtime import (
            first_message_requested,
            is_first_message_job,
            mark_first_message_sent,
        )

        if not is_first_message_job(Path(hermes_home), job_id):
            return silent
        if not first_message_requested(
            relic_home=Path(relic_home), hermes_home=Path(hermes_home)
        ):
            return silent
    except Exception:
        return silent
    try:
        verdict = dependencies.output_critic().review(response_text, consensual=True)
        if not getattr(verdict, "allow", False):
            message = SAFE_OUTPUT_REPLACEMENT
        else:
            message = _text(dependencies.sanitize(response_text)).strip() or SAFE_OUTPUT_REPLACEMENT
    except Exception:
        message = SAFE_OUTPUT_REPLACEMENT
    try:
        mark_first_message_sent(relic_home=Path(relic_home), hermes_home=Path(hermes_home))
    except Exception as exc:  # never block the delivery; log the class name only
        logger.warning("first message delivered but not marked sent: %s", type(exc).__name__)
    return message


def _maintenance_kind_for_session(
    hermes_home: Path | None, session_id: str
) -> str | None:
    if hermes_home is None:
        return None
    match = re.fullmatch(r"cron_(.+)_([0-9]{8})_([0-9]{6})", session_id)
    if match is None:
        return None
    try:
        data = json.loads(
            (Path(hermes_home) / "cron" / "jobs.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    names = {
        "gumi_state_compaction": "state_compaction",
        "gumi_candidate_review": "candidate_review",
    }
    for job in jobs:
        if (
            isinstance(job, dict)
            and (job.get("id") == match.group(1) or job.get("job_id") == match.group(1))
            and isinstance(job.get("name"), str)
        ):
            return names.get(job["name"])
    return None


@contextmanager
def _runtime_scope_env(runtime_scope_id: str, relic_home: str | None) -> Iterator[None]:
    """Bind dependencies to the active User runtime scope."""
    with _RELIC_ENV_LOCK:
        previous_runtime_scope_id = os.environ.get("RELIC_RUNTIME_SCOPE_ID")
        previous_relic_home = os.environ.get("RELIC_HOME")
        os.environ["RELIC_RUNTIME_SCOPE_ID"] = runtime_scope_id
        if relic_home is not None:
            os.environ["RELIC_HOME"] = relic_home
        try:
            yield
        finally:
            if previous_runtime_scope_id is None:
                os.environ.pop("RELIC_RUNTIME_SCOPE_ID", None)
            else:
                os.environ["RELIC_RUNTIME_SCOPE_ID"] = previous_runtime_scope_id
            if previous_relic_home is None:
                os.environ.pop("RELIC_HOME", None)
            else:
                os.environ["RELIC_HOME"] = previous_relic_home


def _runtime_memory_provider(
    dependencies: _RelicDependencies,
    runtime_scope_id: str,
    profile_name: str,
    relic_home: str | None,
) -> Any:
    return dependencies.memory_provider(
        runtime_scope_id=runtime_scope_id,
        hermes_profile_id=profile_name,
        relic_home=relic_home,
    )




def _context_text(result: Any) -> str:
    if isinstance(result, dict):
        return _text(result.get("context")).strip()
    return result.strip() if isinstance(result, str) else ""



def _pending_candidate_context(provider: Any) -> str:
    """Render unconfirmed proposals as explicit review prompts, not memory."""
    pending = getattr(provider, "pending_candidates", None)
    if not callable(pending):
        return ""
    try:
        candidates = pending()
    except Exception:
        return ""
    if not isinstance(candidates, list):
        return ""
    lines = ["[RELIC CONTINUITY CANDIDATES — NOT CONFIRMED]"]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        marker_id = candidate.get("marker_id")
        wording = candidate.get("wording")
        if not isinstance(marker_id, str) or not marker_id.strip():
            continue
        if not isinstance(wording, str) or not wording.strip():
            continue
        marker_id = " ".join(marker_id.split())
        wording = " ".join(wording.split())[:500]
        lines.append(f"- id: {marker_id}; proposed wording: {wording}")
    if len(lines) == 1:
        return ""
    lines.append(
        "Ask the user whether each proposal is accurate. Never confirm or reject "
        "a candidate without explicit user confirmation."
    )
    return "\n".join(lines)

def _checkin_dir(relic_home: str | None) -> Path:
    return (
        Path(relic_home or os.environ.get("RELIC_HOME") or Path.home() / ".relic")
        / "user"
        / "checkin"
    )


def _require_host_api(ctx: Any) -> None:
    # No version marker needed: turn ownership is read from host metadata when
    # present and derived from stock hook arguments otherwise.
    if not callable(getattr(ctx, "register_hook", None)):
        raise RuntimeError("relic-gumi requires Hermes hook registration")


def _maintenance_workspace() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    if not hermes_home:
        raise RuntimeError("HERMES_HOME is required for Gumi maintenance")
    root = Path(hermes_home).expanduser()
    workspace = root / "workspace" / "gumi"
    if workspace.is_symlink() or not workspace.is_dir():
        raise RuntimeError(f"Gumi maintenance workspace is unavailable: {workspace}")
    return workspace


def _read_gumi_canon(args: Any, **_kwargs: Any) -> dict[str, str]:
    payload = args if isinstance(args, dict) else {}
    filename = payload.get("file")
    if filename not in _CANON_FILES:
        raise ValueError("file must name a canonical Gumi file")
    path = _maintenance_workspace() / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"canonical Gumi file is unavailable: {filename}")
    return {
        "status": "ok",
        "file": filename,
        "content": path.read_text(encoding="utf-8"),
    }


def _write_gumi_report(args: Any, **_kwargs: Any) -> dict[str, str]:
    payload = args if isinstance(args, dict) else {}
    report_name = payload.get("report")
    if report_name not in _REPORT_FILES:
        raise ValueError("report must name a Gumi maintenance report")
    content = payload.get("content")
    if not isinstance(content, str) or len(content) > 100_000:
        raise ValueError("maintenance report content is invalid")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("maintenance report must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("maintenance report must be a JSON object")

    workspace = _maintenance_workspace()
    report_path = workspace / _REPORT_FILES[report_name]
    report_dir = report_path.parent
    if report_dir.is_symlink():
        raise RuntimeError(f"Gumi maintenance report directory is unsafe: {report_dir}")
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_dir, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{report_path.name}.", dir=report_dir)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(report_path)
        os.chmod(report_path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"status": "ok", "report": report_name}


def _register_maintenance_tools(register_tool: Callable[..., Any]) -> None:
    register_tool(
        name="gumi_read_canon",
        toolset=MAINTENANCE_TOOLSET,
        schema={
            "name": "gumi_read_canon",
            "description": "Read one of the six canonical Gumi workspace files.",
            "parameters": {
                "type": "object",
                "properties": {"file": {"type": "string", "enum": sorted(_CANON_FILES)}},
                "required": ["file"],
                "additionalProperties": False,
            },
        },
        handler=_read_gumi_canon,
        description="Read only an allowlisted canonical Gumi file.",
    )
    register_tool(
        name="gumi_write_report",
        toolset=MAINTENANCE_TOOLSET,
        schema={
            "name": "gumi_write_report",
            "description": "Write one JSON report owned by a Gumi maintenance job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {"type": "string", "enum": sorted(_REPORT_FILES)},
                    "content": {"type": "string"},
                },
                "required": ["report", "content"],
                "additionalProperties": False,
            },
        },
        handler=_write_gumi_report,
        description="Write only the report assigned to the maintenance job.",
    )


def _register_hooks(
    ctx: Any,
    *,
    runtime_scope_id: str,
    profile_name: str,
    relic_home: str | None,
    dependencies: _RelicDependencies,
    callback_module: str | None = None,
    first_turn_block: Callable[[Mapping[str, Any]], dict[str, str] | None] | None = None,
) -> None:
    with _runtime_scope_env(runtime_scope_id, relic_home):
        provider = _runtime_memory_provider(
            dependencies, runtime_scope_id, profile_name, relic_home
        )
    # Check-in follow-up chain steps planned in pre_llm_call, keyed by session,
    # recorded in post_llm_call once the reply has been captured.
    followup_plans: dict[str, dict[str, Any]] = {}

    admitted_turns = _AdmittedTurns()

    def pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
        if first_turn_block is not None and _foreground_candidate(kwargs):
            # Desktop onboarding: veto the very first FOREGROUND message until
            # /gumi-setup is done (fail-open, only for platform "desktop"). The
            # turn is not admitted: a blocked turn is not ours to touch later.
            blocked = first_turn_block(kwargs)
            if blocked:
                return blocked
        if not _admit_user_turn(kwargs, admitted_turns):
            logger.info(
                "pre_llm_call: not a foreground user turn (platform=%r execution_context=%r user_visible=%r)",
                kwargs.get("platform"), kwargs.get("execution_context"), kwargs.get("user_visible"),
            )
            return None
        user_message = _text(kwargs.get("user_message"))
        context_parts: list[str] = []
        followup = None
        with _runtime_scope_env(runtime_scope_id, relic_home):
            continuity = _text(provider.prefetch(user_message)).strip()
            pending = _pending_candidate_context(provider)
            try:
                from relic.checkin.followup_chain import plan_followup

                checkin_dir = _checkin_dir(relic_home)
                followup = plan_followup(
                    checkin_dir / "relic.db",
                    checkin_dir / "delivery_policy.json",
                    user_message,
                )
            except Exception:
                logger.warning("pre_llm_call: follow-up planning failed", exc_info=True)
                followup = None
        if continuity:
            context_parts.append(continuity)
        if pending:
            context_parts.append(pending)
        inject_kwargs = dict(kwargs)
        session_id = _text(inject_kwargs.pop("session_id", ""))
        inject_kwargs.pop("user_message", None)
        with _runtime_scope_env(runtime_scope_id, relic_home):
            injected = dependencies.inject_context(
                session_id=session_id, user_message=user_message, **inject_kwargs
            )
            hermes_home = dependencies.resolve_hermes_home()
            acknowledgement = _text(
                dependencies.pending_ack_instruction(hermes_home)
            ).strip()
        pcp = _context_text(injected)
        if pcp:
            context_parts.append(pcp)
        if acknowledgement:
            context_parts.append(acknowledgement)
        if followup is not None:
            from relic.checkin.followup_chain import CLOSE_INSTRUCTION, INSTRUCTION

            if followup.get("close"):
                context_parts.append(CLOSE_INSTRUCTION)
            else:
                followup_plans[session_id] = followup
                context_parts.append(INSTRUCTION)
        return {"context": "\n\n".join(context_parts)} if context_parts else None

    def post_llm_call(**kwargs: Any) -> None:
        if not _is_admitted_user_turn(kwargs, admitted_turns):
            return None
        assistant_response = _text(kwargs.get("assistant_response"))
        hermes_home = dependencies.resolve_hermes_home()
        if hermes_home is not None:
            if assistant_response.strip():
                dependencies.clear_pending_reply(hermes_home)
            try:
                from relic.profile.user_runtime import record_subject_reply

                record_subject_reply(
                    hermes_home,
                    relic_home=Path(relic_home) if relic_home is not None else None,
                    runtime_scope_id=runtime_scope_id,
                )
            except Exception:
                pass
        candidate_action = kwargs.get("continuity_candidate_action")
        with _runtime_scope_env(runtime_scope_id, relic_home):
            if isinstance(candidate_action, dict):
                marker_id = candidate_action.get("marker_id")
                action = candidate_action.get("action")
                if isinstance(marker_id, str) and marker_id and action in {"confirm", "reject"}:
                    try:
                        (
                            provider.confirm_candidate
                            if action == "confirm"
                            else provider.reject_candidate
                        )(marker_id)
                    except Exception:
                        pass
            provider.sync_turn(_text(kwargs.get("user_message")), assistant_response)
            followup = followup_plans.pop(_text(kwargs.get("session_id")), None)
            if followup is not None:
                try:
                    from relic.checkin.followup_chain import record_followup

                    record_followup(
                        _checkin_dir(relic_home) / "relic.db", followup, assistant_response
                    )
                except Exception:
                    pass
        return None

    def transform_llm_output(**kwargs: Any) -> str | None:
        if kwargs.get("platform") == "cron":
            hermes_home = dependencies.resolve_hermes_home()
            session_id = _text(kwargs.get("session_id"))
            maintenance_kind = _maintenance_kind_for_session(hermes_home, session_id)
            if maintenance_kind is not None and hermes_home is not None:
                try:
                    from relic.profile.maintenance import finalize_run

                    finalize_run(
                        Path(hermes_home) / "workspace" / "gumi",
                        maintenance_kind,
                        _text(kwargs.get("response_text")),
                    )
                except Exception as exc:
                    return f"[maintenance finalizer failed: {type(exc).__name__}]"
                return "[SILENT]"
            try:
                from relic.profile.user_runtime import consume_delivery_reservation

                consumed, kind = (
                    consume_delivery_reservation(
                        hermes_home,
                        _text(kwargs.get("session_id")),
                        relic_home=Path(relic_home),
                    )
                    if hermes_home is not None and relic_home is not None
                    else (False, None)
                )
                if consumed and relic_home is not None:
                    try:
                        message_text = _text(kwargs.get("response_text"))
                        if kind == "diegetic":
                            message_text = _ensure_diegetic_question_mark(message_text)
                        if os.environ.get("_RELIC_MANUAL_DRYRUN") != "1":
                            if _spawn_dispatch(
                                message_text, kind, hermes_home, relic_home, runtime_scope_id
                            ):
                                return "[SILENT]"
                            if kind == "checkin":
                                _discard_unconfirmed_checkin(relic_home)
                            return _first_message_output(
                                dependencies, hermes_home, relic_home,
                                _text(kwargs.get("session_id")), message_text,
                            )
                        dryrun = _dispatch_and_record(
                            message_text,
                            kind,
                            hermes_home,
                            relic_home,
                            runtime_scope_id,
                        )
                        if dryrun is not None:
                            return dryrun
                    except Exception:
                        if kind == "checkin":
                            _discard_unconfirmed_checkin(relic_home)
                        pass
            except Exception:
                pass
            return _first_message_output(
                dependencies,
                hermes_home,
                relic_home,
                _text(kwargs.get("session_id")),
                _text(kwargs.get("response_text")),
            )
        if not _is_admitted_user_turn(kwargs, admitted_turns):
            return None
        response_text = _text(kwargs.get("response_text"))
        if not response_text:
            return None
        try:
            verdict = dependencies.output_critic().review(response_text, consensual=True)
            if not getattr(verdict, "allow", False):
                return SAFE_OUTPUT_REPLACEMENT
            sanitized_text = _text(dependencies.sanitize(response_text)).strip()
            if not sanitized_text:
                return SAFE_OUTPUT_REPLACEMENT
            return None if sanitized_text == response_text else sanitized_text
        except Exception:
            return SAFE_OUTPUT_REPLACEMENT

    if callback_module:
        for callback in (pre_llm_call, post_llm_call, transform_llm_output):
            callback.__module__ = callback_module
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        _register_maintenance_tools(register_tool)

        def continuity_candidate_action(args: Any, **_kwargs: Any) -> dict[str, str]:
            payload = args if isinstance(args, dict) else {}
            marker_id = payload.get("marker_id")
            action = payload.get("action")
            if (
                not isinstance(marker_id, str)
                or not marker_id
                or action not in {"confirm", "reject"}
            ):
                return {"status": "ignored", "reason": "invalid_request"}
            try:
                with _runtime_scope_env(runtime_scope_id, relic_home):
                    result = (
                        provider.confirm_candidate
                        if action == "confirm"
                        else provider.reject_candidate
                    )(marker_id)
                return {
                    "status": (
                        "confirmed"
                        if action == "confirm" and result is not None
                        else "rejected"
                        if action == "reject" and result is not None
                        else "not_found"
                    ),
                    "marker_id": marker_id,
                }
            except Exception:
                return {"status": "ignored", "reason": "store_unavailable"}

        register_tool(
            name="relic_continuity_candidate",
            toolset=PLUGIN_KEY,
            schema={
                "name": "relic_continuity_candidate",
                "description": (
                    "Confirm or reject a pending Relic continuity candidate. "
                    "Use only after explicit user confirmation; never infer consent."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "marker_id": {
                            "type": "string",
                            "description": "Exact candidate marker identifier.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["confirm", "reject"],
                        },
                    },
                    "required": ["marker_id", "action"],
                    "additionalProperties": False,
                },
            },
            handler=continuity_candidate_action,
            description=(
                "Confirm or reject a pending Relic continuity candidate only "
                "after explicit user confirmation."
            ),
        )


def register(
    ctx: Any,
    *,
    dependencies_loader: Callable[[], _RelicDependencies] = _load_relic_dependencies,
    callback_module: str | None = None,
) -> None:
    _require_host_api(ctx)
    relic_home = os.environ.get("RELIC_HOME") or None
    # Onboarding first: it must be available precisely when no User profile exists yet.
    first_turn_block = _register_onboarding(ctx, relic_home, callback_module)
    try:
        runtime_scope_id, profile_name = _load_user_runtime(relic_home)
    except RuntimeError as exc:
        # Catalog/fresh installs have no profile until setup completes. Never raise from
        # register(); `relic-gumi verify` still fails loudly on the missing hooks.
        logger.warning(
            "relic-gumi: %s; continuity hooks stay disabled until /gumi-setup "
            "(or `relic-gumi install`) completes and Hermes restarts",
            exc,
        )
        return
    _register_hooks(
        ctx,
        runtime_scope_id=runtime_scope_id,
        profile_name=profile_name,
        relic_home=relic_home,
        dependencies=dependencies_loader(),
        callback_module=callback_module,
        first_turn_block=first_turn_block,
    )


def _register_onboarding(
    ctx: Any, relic_home: str | None, callback_module: str | None
) -> Callable[[Mapping[str, Any]], dict[str, str] | None] | None:
    """Telegram + Desktop onboarding, both optional: a failure must never break the plugin.

    Deliberate policy: at runtime this stays fail-soft (an onboarding error must not
    break existing chats), while ``REQUIRED_HOOKS`` (hermes_plugin/install.py) makes
    ``relic-gumi verify`` and install fail LOUDLY when ``pre_gateway_dispatch`` is
    not registered. Operators see the problem at install/verify time, users never
    see a broken bot because of it.

    ``/gumi-setup`` stays ONE command: the Desktop registration builds the
    controller and its ``pre_llm_call`` first-turn block, then the Telegram
    registration registers the command with the routed handler, which picks
    the Desktop path only when the dispatcher's ``platform`` is ``desktop``.
    """
    try:
        from relic.hermes_plugin import desktop_onboarding, telegram_onboarding
        from relic.paths import get_relic_home
        from relic.user_runtime import _hermes_profile_home

        profile_home = _hermes_profile_home(os.environ)
        profiles_home = profile_home.parent
        onboarding_home = Path(relic_home) if relic_home else get_relic_home()
        desktop_controller = desktop_onboarding.register_desktop_onboarding(
            ctx,
            relic_home=onboarding_home,
            profiles_home=profiles_home,
            hermes_base_home=profiles_home.parent,
            env=os.environ,
        )
        telegram_onboarding.register_onboarding(
            ctx,
            relic_home=onboarding_home,
            profiles_home=profiles_home,
            hermes_base_home=profiles_home.parent,
            env=os.environ,
            callback_module=callback_module,
            command_handler=desktop_onboarding.routed_setup_command(
                desktop_controller=desktop_controller
            ),
        )
        return desktop_controller.first_turn_block
    except Exception as exc:
        logger.warning("relic onboarding unavailable: %s", type(exc).__name__)
        return None


def _run_dispatch_worker() -> None:
    """Read a bounded private envelope; argv never contains conversation text."""
    import sys

    try:
        raw = sys.stdin.buffer.read(_DISPATCH_PAYLOAD_LIMIT + 1)
        if len(raw) > _DISPATCH_PAYLOAD_LIMIT:
            return
        envelope = json.loads(raw)
        job, credentials = envelope["job"], envelope["credentials"]
        if not isinstance(job, dict) or not isinstance(credentials, dict):
            return
        job["hermes_home"] = Path(job["hermes_home"])
        job["started_at"] = datetime.fromisoformat(job["started_at"])
    except (ValueError, TypeError, KeyError):
        return  # Never print a malformed payload or exception containing it.
    _dispatch_scoped_job(job, credentials)


if __name__ == "__main__":
    _run_dispatch_worker()
