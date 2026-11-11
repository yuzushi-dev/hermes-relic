"""Manual execution of the installed Hermes job, with an isolated dry-run."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from relic.hermes_runtime import HERMES_USER_PROFILE_NAME


def _copy_state(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise ValueError("dryrun refuses symlinks in runtime state")

    def copy_file(src, dst):
        if Path(src).suffix == ".db":
            # Even SQLite mode=ro updates live WAL shared-memory read marks.
            # Copy a stable DB+WAL pair instead, never opening the live database.
            source_wal, target_wal = Path(str(src) + "-wal"), Path(str(dst) + "-wal")
            def signature(path):
                try:
                    stat = path.stat()
                    return stat.st_ino, stat.st_size, stat.st_mtime_ns
                except FileNotFoundError:
                    return None
            for _ in range(3):
                before = (signature(Path(src)), signature(source_wal))
                try:
                    shutil.copy2(src, dst)
                    if before[1] is not None:
                        shutil.copy2(source_wal, target_wal)
                    elif target_wal.exists():
                        target_wal.unlink()  # Only an abandoned temporary snapshot.
                except FileNotFoundError:
                    continue
                if before == (signature(Path(src)), signature(source_wal)):
                    return dst
            raise ValueError("runtime database changed while copying; retry dryrun")
        return shutil.copy2(src, dst)

    shutil.copytree(source, target, copy_function=copy_file,
                    ignore=shutil.ignore_patterns(".env", "auth.json", "*-wal", "*-shm", "*.lock", "*.sock", "__pycache__"))


@contextmanager
def isolated_state(relic_home: Path, profile_home: Path):
    """Copy private state, including coherent SQLite snapshots; never open live DBs writable."""
    with tempfile.TemporaryDirectory(prefix="relic-cron-dryrun-") as temporary:
        root = Path(temporary)
        relic = root / "relic"
        profile = root / "hermes" / "profiles" / HERMES_USER_PROFILE_NAME
        _copy_state(relic_home / "user", relic / "user")
        _copy_state(profile_home, profile)
        replacements = ((str(profile_home), str(profile)), (str(relic_home), str(relic)))
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".yaml", ".yml", ".md", ".sh"}:
                content = path.read_text(encoding="utf-8")
                remapped = content
                for original, copied in replacements:
                    remapped = remapped.replace(original, copied)
                if remapped != content:
                    path.write_text(remapped, encoding="utf-8")
        (root / "tmp").mkdir()
        yield relic, profile, root


def dryrun_audit(root: Path, endpoints: set[tuple[str, int]]):
    """Fail closed on writes outside the snapshot and network other than model inference."""
    root = root.resolve()

    def writable(path):
        if isinstance(path, int) or path is None:
            return
        path = Path(os.fsdecode(path)).resolve()
        if path != Path(os.devnull) and not path.is_relative_to(root):
            raise PermissionError(f"dryrun blocked write outside temporary state: {path}")

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if not isinstance(address, tuple) or tuple(address[:2]) not in endpoints:
                raise PermissionError("dryrun blocked non-model network access")
        elif event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                writable(path)
        elif event == "sqlite3.connect":
            path = str(args[0])
            if path != ":memory:":
                writable(path.removeprefix("file:").split("?", 1)[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            writable(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            writable(args[0])
            writable(args[1])
    return audit


def install_dryrun_guard() -> None:
    root = Path(os.environ["_RELIC_MANUAL_ROOT"])
    url = urlsplit(os.environ["_RELIC_MANUAL_ENDPOINT"])
    if not url.hostname or url.scheme not in {"http", "https"}:
        raise ValueError("dryrun requires an explicit model endpoint")
    port = url.port or (443 if url.scheme == "https" else 80)
    endpoints = {(row[4][0], port) for row in socket.getaddrinfo(url.hostname, port, type=socket.SOCK_STREAM)}
    sys.dont_write_bytecode = True
    sys.addaudithook(dryrun_audit(root, endpoints))


def run_manual(*, action: str, relic_home: Path, runtime_scope_id: str, force: bool, dryrun: bool) -> dict:
    from relic.profile.user_runtime import CanonicalActionError, _load_profile

    registry, user = _load_profile(relic_home)
    if user.runtime_scope_id != runtime_scope_id:
        raise CanonicalActionError("runtime scope does not match the canonical profile")
    profile_home = registry.hermes_user_home
    context = isolated_state(relic_home, profile_home) if dryrun else nullcontext((relic_home, profile_home, None))
    try:
        with context as (relic, profile, root):
            env = dict(os.environ)
            for name in ("RELIC_FORCE_CHECKIN", "RELIC_FORCE_MEDIA_TYPE", "RELIC_SUBJECT_ID", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env.pop(name, None)
            env.update(RELIC_HOME=str(relic), HERMES_HOME=str(profile), HERMES_PROFILES_HOME=str(profile.parent),
                       RELIC_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1",
                       _RELIC_MANUAL_FORCE="1" if force else "0", _RELIC_MANUAL_DRYRUN="1" if dryrun else "0")
            if dryrun:
                import yaml

                cfg = yaml.safe_load((profile / "config.yaml").read_text()) or {}
                endpoint = (cfg.get("model") or {}).get("base_url")
                jobs_path = profile / "cron/jobs.json"
                if jobs_path.exists():
                    for job in json.loads(jobs_path.read_text()).get("jobs", []):
                        if job.get("name") == "gumi_" + action.replace("-", "_"):
                            endpoint = job.get("base_url") or endpoint
                cfg["mcp_servers"] = {}
                cfg["plugins"] = {"enabled": ["relic-gumi"]}
                (profile / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True))
                env.update(_RELIC_MANUAL_ROOT=str(root), _RELIC_MANUAL_ENDPOINT=endpoint or "",
                           TMPDIR=str(root / "tmp"), TERMINAL_CWD=str(profile / "workspace/gumi"))
            result = subprocess.run(
                [sys.executable, "-m", __name__, "--worker", "--action", action],
                env=env, cwd=profile, capture_output=True, text=True, timeout=1800,
            )
            try:
                report = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise CanonicalActionError("manual cron worker failed: " + result.stderr[-2000:]) from None
            if result.returncode and report.get("ok") is not False:
                raise CanonicalActionError("manual cron worker exited unsuccessfully")
            return report
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise CanonicalActionError(str(exc)) from exc


def _worker(action: str) -> dict:
    dryrun = os.environ.get("_RELIC_MANUAL_DRYRUN") == "1"
    if dryrun:
        install_dryrun_guard()
    profile = Path(os.environ["HERMES_HOME"])
    relic = Path(os.environ["RELIC_HOME"])
    name = "gumi_" + action.replace("-", "_")
    manifest = json.loads((relic / "user/managed_cron.json").read_text())
    expected_id = manifest["jobs"][name]["id"]
    jobs = json.loads((profile / "cron/jobs.json").read_text())["jobs"]
    matches = [job for job in jobs if job.get("name") == name and job.get("id") == expected_id]
    if len(matches) != 1:
        raise ValueError("canonical message job missing or ambiguous")
    job = dict(matches[0])
    if job.get("script") != name + ".sh" or job.get("no_agent") or job.get("enabled_toolsets") != ["none"]:
        raise ValueError("manual execution requires the canonical no-tools message job")
    # Same runner, prompt, provider, reasoning pin and hooks as the gateway.
    # run_job does not schedule a fire or call the scheduler's delivery function.
    with redirect_stdout(io.StringIO()):
        from cron.scheduler import run_job

        success, _document, response, error = run_job(job)
    report = {"ok": success, "job": name, "dryrun": dryrun, "force": os.environ.get("_RELIC_MANUAL_FORCE") == "1"}
    if error:
        report["error"] = error
    if dryrun and response.strip() != "[SILENT]":
        try:
            detail = json.loads(response)
            if detail.get("dryrun") is not True or "dispatch" not in detail:
                raise ValueError
            report.update(detail)
            report["ok"] = success and bool(detail["dispatch"].get("success"))
            if (not error
                    and detail.get("response", "").strip() == "[SILENT]"
                    and detail["dispatch"].get("reason") == "sanitized_empty"):
                report.update(ok=True, status="silent")
        except (ValueError, AttributeError):
            report.update(ok=False, error="dryrun did not reach the simulated dispatcher")
    else:
        report["status"] = "silent" if response.strip() == "[SILENT]" else "completed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--action", choices=("checkin-message", "proactive-message", "diegetic-message"), required=True)
    args = parser.parse_args()
    try:
        report = _worker(args.action)
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
