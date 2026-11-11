"""Canonical, manifest-owned Hermes cron reconciliation for the singleton User."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from relic.hermes_plugin.lifecycle import write_private_json
from relic.profile.user_runtime import CANONICAL_ACTIONS



class ManagedCronError(RuntimeError):
    """A canonical cron operation could not be completed safely."""


PromptFactory = Callable[[], str]
ScriptRenderer = Callable[[Path], str]
CommandRunner = Callable[[tuple[str, ...]], Any]


@dataclass(frozen=True)
class CronSpec:
    name: str
    schedule: str
    prompt: PromptFactory
    script: ScriptRenderer | None
    no_agent: bool = False
    delivery_dependent: bool = False
    delivery_target: str | None = None
    enabled_toolsets: tuple[str, ...] = ("none",)


@dataclass(frozen=True)
class CronReconciliation:
    actions: tuple[str, ...]
    created_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ResolvedSpec:
    spec: CronSpec
    prompt: str
    script: str

    @property
    def delivery_target(self) -> str:
        return self.spec.delivery_target or ""

    def fingerprint(self) -> str:
        data = {
            "name": self.spec.name,
            "schedule": self.spec.schedule,
            "prompt": self.prompt,
            "script": self.script,
            "no_agent": self.spec.no_agent,
            "delivery_target": self.delivery_target,
            "enabled_toolsets": self.spec.enabled_toolsets,
            "repeat": 0,
        }
        return hashlib.sha256(
            json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def create_args(self) -> tuple[str, ...]:
        args = ["cron", "create"]
        if self.spec.no_agent:
            args.append("--no-agent")
        if self.script:
            args.extend(("--script", self.script))
        args.append(self.spec.schedule)
        if self.prompt:
            args.append(self.prompt)
        args.extend(("--name", self.spec.name))
        if self.delivery_target:
            args.extend(("--deliver", self.delivery_target))
        args.extend(("--repeat", "0"))
        return tuple(args)

    def update_args(self, job_id: str) -> tuple[str, ...]:
        args = [
            "cron",
            "edit",
            job_id,
            "--schedule",
            self.spec.schedule,
            "--prompt",
            self.prompt,
            "--name",
            self.spec.name,
            "--deliver",
            self.delivery_target,
            "--repeat",
            "0",
            "--script",
            self.script,
        ]
        if self.spec.no_agent:
            args.append("--no-agent")
        return tuple(args)


@dataclass(frozen=True)
class _CronJob:
    id: str
    name: str
    schedule: str
    prompt: str
    script: str
    no_agent: bool
    delivery_target: str
    repeat: int
    enabled_toolsets: tuple[str, ...]

    def fingerprint(self) -> str:
        data = {
            "name": self.name,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "script": self.script,
            "no_agent": self.no_agent,
            "delivery_target": self.delivery_target,
            "repeat": self.repeat,
            "enabled_toolsets": self.enabled_toolsets,
        }
        return hashlib.sha256(
            json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class _OwnedJob:
    id: str
    fingerprint: str


_CHECKIN_PROMPT = (
    "Read the pre-run gate output JSON; its \"gate\" field is the delivery gate. "
    "Unless it permits delivery, respond only [SILENT]. "
    "This job is a CHECK-IN, not a proactive share and not a diegetic life fragment. "
    "If the Script Output JSON includes a \"checkin_context\" key, treat it as a private "
    "topic hint: use it only to choose what to talk about; never reuse its wording, "
    "spectrum framing, or poles. "
    "The '--- spunto di conversazione (solo per orientarti, non riprenderlo letteralmente) ---' "
    "block is the sole source of the topic. "
    "Recent recipient messages are listening context only. Do not turn the check-in into "
    "a reply to a recent message when that would take you away from the topic. "
    "FOLLOW-UP MODE: if the hint starts with 'Approfondisci', refer to the quoted answer "
    "and deepen that thread by one concrete step; otherwise ask about the fresh topic. "
    "Write the entire message in Italian (italiano), not English. Write one brief warm "
    "message addressed to the recipient and include exactly one "
    "natural question about that topic. Ask about one concrete, recent episode, not about "
    "general tendencies. Keep it close to everyday life: ask about a small moment from today "
    "or the last few days in plain conversational words. Make it answerable in one chat message "
    "without asking the recipient to perform self-analysis. Do not ask for a life lesson, "
    "perspective shift, or defining event; avoid vague placeholders such as 'something' or "
    "'things'. Never name a facet, spectrum, or either extreme, and never use a "
    "psychology/clinical term from the hint verbatim (e.g. 'bias', 'valori fondamentali') even "
    "when the hint has no spectrum — translate the abstract topic into a concrete everyday "
    "question instead of dropping it or quoting its jargon. Example: a hint like "
    "'Dominanza Sistema 1: intuitivo/rapido vs deliberato/analitico. Spettro: deliberato/analitico "
    "↔ intuitivo/rapido.' is not askable as-is, but becomes concrete as something like "
    "'Oggi ti è capitato di decidere qualcosa di getto invece di pensarci su? Cosa stavi facendo?' "
    "— no facet name, no spectrum, no poles, just one everyday moment. Another example: a "
    "no-spectrum hint like 'Modi di pensare o abitudini mentali notate nel tempo' becomes "
    "something like 'C'è stata una scelta di questi giorni che, ripensandoci, avresti fatto "
    "diversamente?' — no jargon word like 'bias' or 'pattern cognitivo', just the everyday moment "
    "it points to. For reciprocity, "
    "for a fresh question, first offer a small, concrete, truthful self-share from Gumi's "
    "world, then ask. If the hint is a follow-up grounded in the recipient's quoted answer, "
    "refer to that answer and deepen it by one step toward a concrete recent episode; the "
    "self-share is optional. Vary the wording and rhythm of that self-share across check-ins "
    "rather than repeating the same phrasing. Include exactly one "
    "open question and exactly one '?'. Never turn the whole message into a standalone self-share, "
    "atmospheric scene, or generic wish. Never mention headings, internal notes, "
    "facets, filters, or scheduling. "
    "If the topic block is present, you MUST ask a question built from it, following the "
    "translation guidance above — [SILENT] is never a valid response to stylistic uncertainty "
    "about how to phrase the question. Respond exactly [SILENT] only when the gate denies "
    "delivery, or when the Script Output JSON has no \"checkin_context\" key at all. "
    "Never reopen a thread listed under '--- argomenti in pausa ---'. Unless you respond "
    "[SILENT], add as the very last line `argomento: <1-2 Italian keywords naming the "
    "concrete thread you asked about>`; it is a private tag and is never sent."
)
_GUMI_WORKSPACE = "$HERMES_HOME/workspace/gumi"

_GUMI_CANON_FILES: tuple[str, ...] = (
    "world.md",
    "voice_canon.json",
    "visual_canon.json",
    "lyria_canon.json",
    "media_policy.json",
    "relationship_policy.md",
)

_COMPACTION_OUTPUT = f"{_GUMI_WORKSPACE}/cron/world_state_compaction_report.json"
_CANDIDATE_REVIEW_OUTPUT = f"{_GUMI_WORKSPACE}/cron/continuity_candidate_review_report.json"
_MEMORY_EXPOSURE_ROLLUP_OUTPUT = f"{_GUMI_WORKSPACE}/cron/memory_exposure_log_rollup_report.json"
_PROVIDER_EVAL_ROLLUP_OUTPUT = f"{_GUMI_WORKSPACE}/cron/provider_eval_metric_rollup_report.json"

_COMPACTION_PROMPT = (
    "Audit the six canonical Gumi files from the bounded JSON in Script Output for "
    "stale, redundant, or bloated entries: world.md, voice_canon.json, "
    "visual_canon.json, lyria_canon.json, media_policy.json, relationship_policy.md. "
    "Do not call tools or search files. Do NOT modify canon files. Missing files are candidates. "
    "Return exactly one JSON object, with no markdown or prose, copying the exact "
    "\"snapshot_id\" from Script Output and using \"status\" (ok|candidates_found) "
    "and \"candidates\" ([{file,note}])."
)
_CANDIDATE_REVIEW_PROMPT = (
    "Review consistency and drift across the six canonical Gumi files from the bounded "
    "JSON in Script Output: world.md, voice_canon.json, visual_canon.json, "
    "lyria_canon.json, media_policy.json, relationship_policy.md. Do not call tools or "
    "search files. Do NOT modify canon files. Record missing or inconsistent files as drift. "
    "Return exactly one JSON object, with no markdown or prose, copying the exact "
    "\"snapshot_id\" from Script Output and using \"status\" (ok|drift) and "
    "\"findings\" ([string]). The findings list must be empty when status is ok; "
    "include only actionable inconsistencies or missing files when status is drift."
)


def _empty_prompt() -> str:
    return ""


def _checkin_prompt() -> str:
    return _CHECKIN_PROMPT


def _proactive_prompt() -> str:
    from relic.gumi_plugin.cron_wiring import render_proactive_message_prompt

    return render_proactive_message_prompt()


def _diegetic_prompt() -> str:
    from relic.gumi_plugin.cron_wiring import render_diegetic_message_prompt

    return render_diegetic_message_prompt()


def _compaction_prompt() -> str:
    return _COMPACTION_PROMPT


def _candidate_review_prompt() -> str:
    return _CANDIDATE_REVIEW_PROMPT


def _memory_exposure_rollup_prompt() -> str:
    return (
        "Aggregate and redact memory exposure events for the Gumi user. "
        "Do not access raw chat logs, MEMORY.md, or USER.md. "
        "If there are no exposure events to aggregate, or the memory subsystem/tool "
        "is unavailable in this environment, treat it as a successful no-op: respond "
        "exactly [SILENT] (do NOT report this as a failure). "
        f"Otherwise write the rollup to {_MEMORY_EXPOSURE_ROLLUP_OUTPUT} and respond exactly "
        "[SILENT] on success; return a short diagnostic only for a genuine processing error."
    )


def _provider_eval_rollup_prompt() -> str:
    return (
        "Aggregate provider evaluation metrics for the Gumi user. "
        "Do not call any provider API or mutate any profile file. "
        f"Write the rollup to {_PROVIDER_EVAL_ROLLUP_OUTPUT}. On success respond exactly "
        "[SILENT]; on failure return a short diagnostic."
    )


def _canonical_action_script(action: str) -> ScriptRenderer:
    if action not in CANONICAL_ACTIONS:
        raise ValueError(f"unsupported canonical action: {action}")

    _python_fallback = shlex.quote(sys.executable)
    manual = action in {"checkin-message", "proactive-message", "diegetic-message"}
    argument_guard = '''if [[ "$#" -gt 2 ]]; then
    printf '%s\\n' 'CANONICAL_ACTION_ERROR: expected at most --force and --dryrun' >&2
    exit 64
fi
for argument in "$@"; do
    case "$argument" in
        --force|--dryrun|--help) ;;
        *) printf '%s\\n' 'CANONICAL_ACTION_ERROR: expected --force or --dryrun' >&2; exit 64 ;;
    esac
done''' if manual else '''if [[ "$#" -ne 0 ]]; then
    printf '%s\\n' 'CANONICAL_ACTION_ERROR: generated action accepts no arguments' >&2
    exit 64
fi'''

    def render(_path: Path) -> str:
        return f"""#!/usr/bin/env bash
set -euo pipefail

{argument_guard}

RELIC_HOME="${{RELIC_HOME:-$HOME/.relic}}"
PROFILE_FILE="$RELIC_HOME/user/user_profile.json"
if [[ ! -f "$PROFILE_FILE" || -L "$PROFILE_FILE" ]]; then
    printf '%s\n' 'CANONICAL_ACTION_ERROR: canonical profile is unavailable' >&2
    exit 1
fi
if ! RELIC_RUNTIME_SCOPE_ID="$("${{RELIC_PYTHON:-{_python_fallback}}}" - "$PROFILE_FILE" <<'PY'
import json
import re
import stat
import sys
from pathlib import Path

profile_path = Path(sys.argv[1])
try:
    mode = profile_path.lstat().st_mode
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise ValueError
    with profile_path.open(encoding="utf-8") as stream:
        profile = json.load(stream)
    runtime_scope_id = profile.get("runtime_scope_id") if isinstance(profile, dict) else None
    if not isinstance(runtime_scope_id, str) or re.fullmatch(r"[0-9a-f]{{64}}", runtime_scope_id) is None:
        raise ValueError
except (OSError, TypeError, ValueError, json.JSONDecodeError):
    print("CANONICAL_ACTION_ERROR: canonical profile is invalid", file=sys.stderr)
    raise SystemExit(1)
print(runtime_scope_id)
PY
)"; then
    exit 1
fi
export RELIC_RUNTIME_SCOPE_ID
# Resolve the profile-local bundle relative to this script, not the installer
# checkout or a PM environment generation that may have been replaced.
RELIC_VENDOR="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)/../plugins/relic-gumi/_vendor"
if [[ -d "$RELIC_VENDOR/relic" ]]; then
    export PYTHONPATH="$RELIC_VENDOR${{PYTHONPATH:+:$PYTHONPATH}}"
fi
exec "${{RELIC_PYTHON:-{_python_fallback}}}" -m relic.profile.user_runtime \
    --relic-home "$RELIC_HOME" \
    --runtime-scope-id "$RELIC_RUNTIME_SCOPE_ID" \
    --action "{action}" "$@"
"""

    return render



MANAGED_CRON_SPECS: tuple[CronSpec, ...] = (
    CronSpec(
        "gumi_memory_sync",
        "2-59/30 * * * *",
        _empty_prompt,
        _canonical_action_script("continuity-sync"),
        no_agent=True,
    ),
    CronSpec(
        "gumi_checkin_gate",
        "*/30 * * * *",
        _empty_prompt,
        _canonical_action_script("checkin-gate"),
        no_agent=True,
    ),
    CronSpec(
        "gumi_checkin_message",
        "*/30 * * * *",
        _checkin_prompt,
        _canonical_action_script("checkin-message"),
        delivery_dependent=True,
        delivery_target="local",
    ),
    CronSpec(
        "gumi_diegetic_dispatch",
        "17 * * * *",
        _empty_prompt,
        _canonical_action_script("continuity-dispatch"),
        no_agent=True,
        delivery_dependent=True,
    ),
    CronSpec(
        "gumi_diegetic_gate",
        "20 * * * *",
        _empty_prompt,
        _canonical_action_script("diegetic-gate"),
        no_agent=True,
    ),
    CronSpec(
        "gumi_diegetic_message",
        "20 * * * *",
        _diegetic_prompt,
        _canonical_action_script("diegetic-message"),
        delivery_dependent=True,
        delivery_target="local",
    ),
    CronSpec(
        "gumi_proactive_gate",
        "45 * * * *",
        _empty_prompt,
        _canonical_action_script("proactive-gate"),
        no_agent=True,
    ),
    CronSpec(
        "gumi_proactive_message",
        "45 * * * *",
        _proactive_prompt,
        _canonical_action_script("proactive-message"),
        delivery_dependent=True,
        delivery_target="local",
    ),
    CronSpec(
        "gumi_state_compaction",
        "0 3 * * *",
        _compaction_prompt,
        _canonical_action_script("continuity-compaction"),
        delivery_target="local",
    ),
    CronSpec(
        "gumi_candidate_review",
        "0 4 * * *",
        _candidate_review_prompt,
        _canonical_action_script("continuity-review"),
        delivery_target="local",
    ),
    CronSpec(
        "gumi_memory_exposure_rollup",
        "0 5 * * 0",
        _memory_exposure_rollup_prompt,
        _canonical_action_script("memory-exposure-rollup"),
        delivery_target="local",
    ),
    CronSpec(
        "gumi_provider_eval_rollup",
        "0 6 * * 0",
        _provider_eval_rollup_prompt,
        _canonical_action_script("provider-eval-rollup"),
        delivery_target="local",
    ),
)


class ManagedCron:
    """Reconcile exactly the cron jobs recorded in one private manifest."""

    def __init__(
        self,
        *,
        base_home: Path,
        runner: CommandRunner,
        specs: Sequence[CronSpec] = MANAGED_CRON_SPECS,
        scripts_dir: Path | None = None,
    ) -> None:
        self.base_home = Path(base_home).expanduser()
        self.runner = runner
        self.specs = tuple(specs)
        self.scripts_dir = (
            Path(scripts_dir).expanduser()
            if scripts_dir is not None
            else self.base_home / "user" / "scripts"
        )
        self.hermes_jobs_path: Path | None = None
        names = tuple(spec.name for spec in self.specs)
        if len(names) != len(set(names)):
            raise ValueError("Managed cron spec names must be unique")

    @property
    def manifest_path(self) -> Path:
        return self.base_home / "user" / "managed_cron.json"

    def configure_hermes_jobs_path(self, profile_home: Path) -> None:
        self.hermes_jobs_path = Path(profile_home).expanduser() / "cron" / "jobs.json"

    def reconcile(self, *, delivery_armed: bool) -> CronReconciliation:
        if not isinstance(delivery_armed, bool):
            raise TypeError("delivery_armed must be bool")
        self._ensure_private_directory(self.manifest_path.parent)
        owned = self._load_manifest()
        jobs = self._list_jobs()
        by_id = {job.id: job for job in jobs}
        next_owned = dict(owned)
        actions: list[str] = []
        created: list[str] = []
        try:
            for spec in self.specs:
                existing = owned.get(spec.name)
                wanted = delivery_armed or not spec.delivery_dependent
                same_name = [job for job in jobs if job.name == spec.name]
                if not wanted:
                    if existing is not None:
                        job = by_id.get(existing.id)
                        if job is not None:
                            self._remove(job.id, spec.name)
                            jobs.remove(job)
                            by_id.pop(job.id, None)
                            actions.append(f"removed:{spec.name}")
                        else:
                            actions.append(f"absent:{spec.name}")
                        next_owned.pop(spec.name, None)
                    continue
                resolved = self._resolve(spec)
                if existing is None:
                    if same_name:
                        self._collision(spec.name)
                    job_id = self._create(resolved)
                    created.append(job_id)
                    next_owned[spec.name] = _OwnedJob(job_id, resolved.fingerprint())
                    actions.append(f"created:{spec.name}")
                    continue
                job = by_id.get(existing.id)
                if any(candidate.id != existing.id for candidate in same_name):
                    self._collision(spec.name)
                if job is None:
                    if same_name:
                        self._collision(spec.name)
                    job_id = self._create(resolved)
                    created.append(job_id)
                    next_owned[spec.name] = _OwnedJob(job_id, resolved.fingerprint())
                    actions.append(f"created:{spec.name}")
                    continue
                expected = resolved.fingerprint()
                if existing.fingerprint == expected and job.fingerprint() == expected:
                    actions.append(f"unchanged:{spec.name}")
                    continue
                self._update(job.id, resolved, spec.name)
                next_owned[spec.name] = _OwnedJob(job.id, expected)
                actions.append(f"updated:{spec.name}")
            self._write_manifest(next_owned)
        except Exception:
            self._rollback_created(created)
            raise
        return CronReconciliation(tuple(actions), tuple(created))

    def rollback(self, reconciliation: CronReconciliation) -> None:
        """Delete only jobs created by this successful reconciliation."""
        self._rollback_created(reconciliation.created_ids)

    def remove_all(self) -> tuple[str, ...]:
        """Remove every job currently owned by this manifest, unconditionally."""
        owned = self._load_manifest()
        jobs = self._list_jobs()
        by_id = {job.id: job for job in jobs}
        removed: list[str] = []
        for name, entry in owned.items():
            job = by_id.get(entry.id)
            if job is not None:
                self._remove(job.id, name)
            removed.append(name)
        self._write_manifest({})
        return tuple(removed)

    def check_owned(self) -> tuple[str, ...]:
        """Read-only diagnostic: report each owned job's reconciliation state.

        Does not create, update, or remove anything.
        """
        owned = self._load_manifest()
        jobs = self._list_jobs()
        by_id = {job.id: job for job in jobs}
        results: list[str] = []
        for spec in self.specs:
            entry = owned.get(spec.name)
            if entry is None:
                results.append(f"unowned:{spec.name}")
                continue
            job = by_id.get(entry.id)
            if job is None:
                results.append(f"missing:{spec.name}")
                continue
            # Fingerprint only depends on the script's *filename*, not its on-disk
            # content, so this is computed without _resolve()'s disk write - this
            # method must stay read-only.
            script = f"{spec.name}.sh" if spec.script is not None else ""
            expected = _ResolvedSpec(spec, spec.prompt(), script).fingerprint()
            if entry.fingerprint == expected and job.fingerprint() == expected:
                results.append(f"ok:{spec.name}")
            else:
                results.append(f"stale:{spec.name}")
        return tuple(results)

    def _resolve(self, spec: CronSpec) -> _ResolvedSpec:
        script = ""
        if spec.script is not None:
            path = self.scripts_dir / f"{spec.name}.sh"
            self._write_script(path, spec.script(path))
            script = path.name
        return _ResolvedSpec(spec, spec.prompt(), script)

    def _read_job_records(self) -> list[Mapping[str, Any]]:
        """Jobs as Hermes stores them. Stock Hermes has no JSON listing, so read the
        profile's ``cron/jobs.json`` (what ``cron.jobs.list_jobs`` itself reads)."""
        path = self.hermes_jobs_path
        if path is None:
            result = self._execute(("cron", "list", "--json"), "list")
            return _jobs_from_json(result.stdout)
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise ManagedCronError("Hermes cron jobs file is unavailable")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ManagedCronError("Cannot read Hermes cron jobs file") from exc
        return _jobs_from_json(text)

    def _list_jobs(self) -> list[_CronJob]:
        records = self._read_job_records()
        jobs: list[_CronJob] = []
        known_names = {spec.name for spec in self.specs}
        for record in records:
            job = _normalize_job(record)
            if job is None:
                if _string(record.get("name")) in known_names:
                    raise ManagedCronError(f"Cron list returned invalid record for {record['name']}")
                continue
            jobs.append(job)
        return jobs

    def _create(self, spec: _ResolvedSpec) -> str:
        result = self._execute(spec.create_args(), f"create {spec.spec.name}")
        job_id = _created_job_id(result.stdout) or self._created_id_by_name(spec.spec.name)
        if job_id is None:
            raise ManagedCronError(f"Cron create for {spec.spec.name} returned no job id")
        try:
            self._patch_toolsets(job_id, spec.spec.enabled_toolsets)
        except Exception:
            try:
                self._remove(job_id, spec.spec.name)
            except Exception:
                pass
            raise
        return job_id

    def _created_id_by_name(self, name: str) -> str | None:
        if self.hermes_jobs_path is None:
            return None
        matches = [
            job.id for job in map(_normalize_job, self._read_job_records())
            if job is not None and job.name == name
        ]
        return matches[0] if len(matches) == 1 else None

    def _update(self, job_id: str, spec: _ResolvedSpec, name: str) -> None:
        self._execute(spec.update_args(job_id), f"update {name}")
        self._patch_toolsets(job_id, spec.spec.enabled_toolsets)

    def _remove(self, job_id: str, name: str) -> None:
        self._execute(("cron", "remove", job_id), f"delete {name}")

    def _rollback_created(self, job_ids: Sequence[str]) -> None:
        for job_id in reversed(tuple(job_ids)):
            try:
                self._execute(("cron", "remove", job_id), "rollback")
            except ManagedCronError:
                pass

    def _execute(self, command: tuple[str, ...], action: str) -> "_CommandResult":
        try:
            result = _command_result(self.runner(command))
        except Exception as exc:
            raise ManagedCronError(f"Cron {action} command failed") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ManagedCronError(f"Cron {action} failed{f': {detail}' if detail else ''}")
        return result

    def _load_manifest(self) -> dict[str, _OwnedJob]:
        path = self.manifest_path
        if not path.exists():
            return {}
        if path.is_symlink() or not path.is_file():
            raise ManagedCronError(f"Managed cron manifest is not a regular file: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedCronError(f"Cannot read managed cron manifest: {path}") from exc
        if not isinstance(data, Mapping) or data.get("schema_version") != 1:
            raise ManagedCronError(f"Invalid managed cron manifest: {path}")
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, Mapping):
            raise ManagedCronError(f"Invalid managed cron manifest jobs: {path}")
        expected = {spec.name for spec in self.specs}
        owned: dict[str, _OwnedJob] = {}
        for name, record in raw_jobs.items():
            if not isinstance(name, str) or name not in expected or not isinstance(record, Mapping):
                raise ManagedCronError(f"Invalid managed cron manifest entry: {path}")
            job_id = _string(record.get("id"))
            fingerprint = _string(record.get("fingerprint"))
            if not job_id or not fingerprint:
                raise ManagedCronError(f"Invalid managed cron manifest entry: {path}")
            owned[name] = _OwnedJob(job_id, fingerprint)
        return owned

    def _write_manifest(self, owned: Mapping[str, _OwnedJob]) -> None:
        write_private_json(
            self.manifest_path,
            {
                "schema_version": 1,
                "jobs": {
                    name: {"id": job.id, "fingerprint": job.fingerprint}
                    for name, job in sorted(owned.items())
                },
            },
        )

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        ensure_private_directory(path)

    def _write_script(self, path: Path, content: str) -> None:
        write_private_script(path, content)

    def _patch_toolsets(self, job_id: str, toolsets: tuple[str, ...]) -> None:
        path = self.hermes_jobs_path
        if path is None:
            return
        if path.is_symlink() or not path.is_file():
            raise ManagedCronError("Hermes cron jobs file is unavailable")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManagedCronError("Cannot update Hermes cron toolsets") from exc
        jobs = data.get("jobs") if isinstance(data, Mapping) else None
        if not isinstance(jobs, list):
            raise ManagedCronError("Hermes cron jobs file has no jobs list")
        for job in jobs:
            if (
                isinstance(job, dict)
                and (job.get("id") == job_id or job.get("job_id") == job_id)
            ):
                job["enabled_toolsets"] = list(toolsets)
                break
        else:
            raise ManagedCronError(f"Created Hermes cron job is missing: {job_id}")
        mode = path.stat().st_mode & 0o777
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            os.chmod(path, mode)
        finally:
            if temporary.exists():
                temporary.unlink()
        try:
            check = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManagedCronError("Cannot verify Hermes cron toolsets") from exc
        check_jobs = check.get("jobs") if isinstance(check, Mapping) else None
        if not isinstance(check_jobs, list) or not any(
            isinstance(job, Mapping)
            and (job.get("id") == job_id or job.get("job_id") == job_id)
            and job.get("enabled_toolsets") == list(toolsets)
            for job in check_jobs
        ):
            raise ManagedCronError("Hermes cron toolsets were not persisted")

    @staticmethod
    def _collision(name: str) -> None:
        raise ManagedCronError(
            f"Cron job {name} is unmanaged; remove or rename it before installing RELIC Gumi."
        )


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _command_result(value: Any) -> _CommandResult:
    if isinstance(value, tuple) and len(value) == 3:
        return _CommandResult(int(value[0]), str(value[1] or ""), str(value[2] or ""))
    try:
        return _CommandResult(
            int(value.returncode),
            str(getattr(value, "stdout", "") or ""),
            str(getattr(value, "stderr", "") or ""),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ManagedCronError("Cron command returned an invalid result") from exc


def _jobs_from_json(text: str) -> list[Mapping[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ManagedCronError("Cron list --json returned invalid JSON") from exc
    jobs = _extract_jobs(data)
    if jobs is None:
        raise ManagedCronError("Cron list --json did not contain a jobs list")
    return jobs


def _extract_jobs(data: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if not isinstance(data, Mapping):
        return None
    for key in ("jobs", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    for key in ("data", "result"):
        nested = _extract_jobs(data.get(key))
        if nested is not None:
            return nested
    return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CREATED_LINE_RE = re.compile(r"^\s*Created job:\s*(\S+)\s*$", re.MULTILINE)


def ensure_private_directory(path: Path) -> None:
    """Create ``path`` as a 0700 directory, refusing a symlink."""
    if path.is_symlink():
        raise ManagedCronError(f"Managed cron directory is a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private_script(path: Path, content: str) -> None:
    """Atomically write an executable 0700 script into a private directory."""
    ensure_private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o700)
    finally:
        if temporary.exists():
            temporary.unlink()


def _created_job_id(text: str) -> str | None:
    """Job id from ``cron create`` output: JSON, or stock Hermes' ``Created job: <id>``."""
    try:
        return _id_from_data(json.loads(text))
    except json.JSONDecodeError:
        match = _CREATED_LINE_RE.search(_ANSI_RE.sub("", text))
        return match.group(1) if match else None


def _id_from_data(data: Any) -> str | None:
    if isinstance(data, Mapping):
        for key in ("id", "job_id"):
            value = _string(data.get(key))
            if value:
                return value
        for key in ("job", "data", "result", "created_job"):
            value = _id_from_data(data.get(key))
            if value:
                return value
    return None


def _normalize_job(record: Mapping[str, Any]) -> _CronJob | None:
    job_id = _string(record.get("id") or record.get("job_id"))
    name = _string(record.get("name"))
    if not job_id or not name:
        return None
    schedule = record.get("schedule") or record.get("cron")
    if isinstance(schedule, Mapping):
        schedule = schedule.get("expr") or schedule.get("display")
    repeat_value = record.get("repeat", 0)
    if isinstance(repeat_value, Mapping):
        repeat_value = repeat_value.get("times")
    try:
        repeat = 0 if repeat_value is None else int(repeat_value)
    except (TypeError, ValueError):
        repeat = -1
    return _CronJob(
        id=job_id,
        name=name,
        schedule=_text(schedule),
        prompt=_text(record.get("prompt")),
        script=_text(record.get("script")),
        no_agent=bool(record.get("no_agent", record.get("noAgent", False))),
        delivery_target=_text(record.get("deliver") or record.get("delivery") or record.get("target")),
        repeat=repeat,
        enabled_toolsets=_toolsets(record.get("enabled_toolsets", record.get("enabledToolsets"))),
    )


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""

def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _toolsets(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))
