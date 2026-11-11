"""Delivery of the one-shot first message, then marking it sent.

Stock cron turns are silenced by ``transform_llm_output`` unless a managed
reservation exists. The ``relic-gumi:first-message`` job has none, so its
output must be let through (after the user-turn output checks) and the first
message marked ``sent`` so it is never repeated.
"""

from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

import importlib.util
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from relic.profile.registry import ProfileRegistry
from relic.user_runtime import (
    FIRST_MESSAGE_JOB_NAME,
    RuntimeActivator,
    first_message_requested,
)

from tests.test_user_runtime_activation import (
    ManagedCronFactory,
    ManagedCronSpy,
    _save_state,
    _state,
)
from tests.test_user_runtime_stock_job_shape import SCRIPT_NAME, StockShapeRunner

pytestmark = pytest.mark.usefixtures("inline_cron_dispatch")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SAFE_REPLACEMENT = "I'm here with you. What would feel most helpful right now?"
JOB_ID = "firstjob01"
SESSION = f"cron_{JOB_ID}_20261110_101500"


@contextmanager
def _bridge_module():
    module_name = "relic_gumi_bridge_first_message_test"
    spec = importlib.util.spec_from_file_location(module_name, REPOSITORY_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.relic_home = tmp_path
        self.profiles = tmp_path / "hermes" / "profiles"
        self.hermes_home = self.profiles / HERMES_USER_PROFILE_NAME
        monkeypatch.setenv("HERMES_HOME", str(self.hermes_home))
        monkeypatch.delenv("HERMES_PROFILES_HOME", raising=False)
        monkeypatch.setenv("RELIC_HOME", str(tmp_path))
        self.checkin_state_path = tmp_path / "user" / "checkin_state.json"

    def save(self, **kwargs) -> None:
        _save_state(self.relic_home, _state(**kwargs), hermes_profiles_home=self.profiles)

    def write_jobs(self, jobs: list[dict]) -> None:
        path = self.hermes_home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    def status(self) -> str:
        data = json.loads(self.checkin_state_path.read_text(encoding="utf-8"))
        return data["first_message"]["status"]


def _first_message_job(job_id: str = JOB_ID) -> dict:
    return {"id": job_id, "name": FIRST_MESSAGE_JOB_NAME, "deliver": "telegram:123456789"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    e = _Env(tmp_path, monkeypatch)
    e.save(armed=True, first_message="gumi_sends_first")
    e.write_jobs([_first_message_job()])
    return e


def _transform(
    env: _Env,
    *,
    critic_allows: bool = True,
    sanitized: str = "Hi, it's Gumi.",
    resolve_home=None,
    relic_home="__env__",
):
    with _bridge_module() as bridge:
        critic = Mock()
        critic.review.return_value = SimpleNamespace(allow=critic_allows)
        dependencies = SimpleNamespace(
            memory_provider=Mock(),
            inject_context=Mock(return_value={"context": "pcp"}),
            pending_ack_instruction=Mock(return_value=""),
            resolve_hermes_home=resolve_home or Mock(return_value=env.hermes_home),
            clear_pending_reply=Mock(),
            output_critic=Mock(return_value=critic),
            sanitize=Mock(return_value=sanitized),
        )
        ctx = SimpleNamespace(register_hook=Mock())
        bridge._register_hooks(
            ctx,
            runtime_scope_id="runtime-scope",
            profile_name=HERMES_USER_PROFILE_NAME,
            relic_home=str(env.relic_home) if relic_home == "__env__" else relic_home,
            dependencies=dependencies,
        )
        hooks ={call.args[0]: call.args[1] for call in ctx.register_hook.call_args_list}
    return hooks["transform_llm_output"], critic, dependencies


def test_first_message_job_is_delivered_sanitized_not_silent(env: _Env) -> None:
    transform, critic, dependencies = _transform(env, sanitized="Hi, it's Gumi.")

    out = transform(response_text="Hello <raw>", platform="cron", session_id=SESSION)

    assert out == "Hi, it's Gumi."
    critic.review.assert_called_once_with("Hello <raw>", consensual=True)
    dependencies.sanitize.assert_called_once_with("Hello <raw>")


def test_blocking_critic_delivers_the_safe_replacement(env: _Env) -> None:
    transform, _critic, _deps = _transform(env, critic_allows=False)

    assert transform(response_text="bad", platform="cron", session_id=SESSION) == SAFE_REPLACEMENT


def test_sanitizer_emptying_the_text_delivers_the_safe_replacement(env: _Env) -> None:
    transform, _critic, _deps = _transform(env, sanitized="   ")

    assert transform(response_text="x", platform="cron", session_id=SESSION) == SAFE_REPLACEMENT


@pytest.mark.parametrize("response", ["[SILENT]", "  [silent]\n", "", "   ", None])
def test_silent_or_empty_response_stays_silent_and_unsent(env: _Env, response) -> None:
    transform, critic, _deps = _transform(env)

    out = transform(response_text=response, platform="cron", session_id=SESSION)

    assert out == "[SILENT]"
    assert env.status() == "requested"
    critic.review.assert_not_called()


def test_other_cron_job_stays_silent_even_with_similar_name(env: _Env) -> None:
    env.write_jobs(
        [
            _first_message_job(),
            {"id": "other02", "name": FIRST_MESSAGE_JOB_NAME + "-copy"},
            {"id": "other03", "name": "relic-gumi:first-message ", "prompt": "first message"},
            {"id": "firstjob0", "name": "something else"},
        ]
    )
    transform, critic, _deps = _transform(env)

    for session in (
        "cron_other02_20261110_101500",
        "cron_other03_20261110_101500",
        "cron_firstjob0_20261110_101500",  # prefix of the real id
        "cron_firstjob011_20261110_101500",  # real id is a prefix of it
        "cron-job-1",
        "cron_unknown_20261110_101500",
    ):
        assert transform(response_text="hello", platform="cron", session_id=session) == "[SILENT]"
    assert env.status() == "requested"
    critic.review.assert_not_called()


def test_missing_or_symlinked_jobs_file_stays_silent(env: _Env, tmp_path: Path) -> None:
    transform, _critic, _deps = _transform(env)
    jobs = env.hermes_home / "cron" / "jobs.json"
    real = tmp_path / "real_jobs.json"
    jobs.rename(real)
    assert transform(response_text="hello", platform="cron", session_id=SESSION) == "[SILENT]"
    jobs.symlink_to(real)
    assert transform(response_text="hello", platform="cron", session_id=SESSION) == "[SILENT]"
    assert env.status() == "requested"


def test_blocked_gate_stays_silent_when_already_sent(env: _Env) -> None:
    transform, _critic, _deps = _transform(env)
    assert transform(response_text="hi", platform="cron", session_id=SESSION) != "[SILENT]"
    assert env.status() == "sent"

    assert transform(response_text="hi again", platform="cron", session_id=SESSION) == "[SILENT]"


def test_blocked_gate_stays_silent_when_consent_revoked(env: _Env) -> None:
    env.save(armed=True, first_message="wait")  # user turned the first message off
    transform, critic, _deps = _transform(env)

    assert transform(response_text="hi", platform="cron", session_id=SESSION) == "[SILENT]"
    assert env.status() == "waiting"
    critic.review.assert_not_called()


def test_delivery_marks_sent_and_closes_the_gate_exactly_once(env: _Env) -> None:
    transform, _critic, _deps = _transform(env)
    assert first_message_requested(relic_home=env.relic_home) is True

    first = transform(response_text="hi", platform="cron", session_id=SESSION)
    second = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert first != "[SILENT]"
    assert second == "[SILENT]"
    assert env.status() == "sent"
    assert first_message_requested(relic_home=env.relic_home) is False


def test_marking_sent_keeps_permissions_and_other_fields(env: _Env) -> None:
    user_dir = env.relic_home / "user"
    before = {p.name: p.read_bytes() for p in user_dir.glob("*.json")}
    before_state = json.loads(before["checkin_state.json"])
    transform, _critic, _deps = _transform(env)

    transform(response_text="hi", platform="cron", session_id=SESSION)

    for path in user_dir.glob("*.json"):
        if path.name != "checkin_state.json":
            assert path.read_bytes() == before[path.name], path.name
    after_state = json.loads(env.checkin_state_path.read_text(encoding="utf-8"))
    assert after_state["first_message"] == {**before_state["first_message"], "status": "sent"}
    assert {k: v for k, v in after_state.items() if k != "first_message"} == {
        k: v for k, v in before_state.items() if k != "first_message"
    }
    assert stat.S_IMODE(os.stat(env.checkin_state_path).st_mode) == 0o600
    assert not list(user_dir.glob(".*tmp*")) and not list(user_dir.glob(".*rollback*"))
    # canonical artifacts still load with the new status
    registry = ProfileRegistry(relic_home=env.relic_home, hermes_profiles_home=env.profiles)
    configuration = registry.load_user_configuration()
    assert configuration["checkin_state"]["first_message"]["status"] == "sent"


def test_failed_state_write_does_not_block_delivery(
    env: _Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(self, *args, **kwargs):
        raise OSError("disk full: secret-path")

    monkeypatch.setattr(ProfileRegistry, "_replace_private_files", boom)
    transform, _critic, _deps = _transform(env, sanitized="Hi.")

    with caplog.at_level("WARNING"):
        out = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert out == "Hi."
    assert env.status() == "requested"
    assert "OSError" in caplog.text
    assert "secret-path" not in caplog.text


def test_maintenance_and_reservation_branches_are_unchanged(env: _Env) -> None:
    transform, critic, _deps = _transform(env)
    # An unmapped cron turn keeps being silenced and never reaches the critic.
    assert (
        transform(response_text="rogue", platform="cron", session_id="cron_zzz_20261110_101500")
        == "[SILENT]"
    )
    critic.review.assert_not_called()
    assert env.status() == "requested"


# --- Part C: reconciliation after the send --------------------------------


def _activator(env: _Env, runner: StockShapeRunner) -> RuntimeActivator:
    return RuntimeActivator(
        runner=runner,
        relic_home=env.relic_home,
        hermes_base_home=env.relic_home / "hermes",
        environ={},
        managed_cron_factory=ManagedCronFactory(ManagedCronSpy()),
    )


def test_reconcile_after_sent_removes_job_and_script_and_never_recreates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch)
    env.save(armed=True, first_message="gumi_sends_first")
    runner = StockShapeRunner(env.hermes_home)
    assert _activator(env, runner).reconcile().first_message_scheduled is True
    (job,) = [j for j in runner.jobs if j["name"] == FIRST_MESSAGE_JOB_NAME]
    script = env.hermes_home / "scripts" / SCRIPT_NAME
    assert script.is_file()

    transform, _critic, _deps = _transform(env)
    session = f"cron_{job['id']}_20261110_101500"
    assert transform(response_text="hi", platform="cron", session_id=session) != "[SILENT]"
    assert env.status() == "sent"

    activation = _activator(env, runner).reconcile()
    assert activation.first_message_scheduled is False
    assert [j for j in runner.jobs if j["name"] == FIRST_MESSAGE_JOB_NAME] == []
    assert not script.exists()

    runner.calls.clear()
    assert _activator(env, runner).reconcile().first_message_scheduled is False
    assert runner.writes() == []  # nothing is created again


def test_reconcile_with_waiting_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch)
    env.save(armed=True, first_message="wait")
    runner = StockShapeRunner(env.hermes_home)

    activation = _activator(env, runner).reconcile()

    assert activation.first_message_scheduled is False
    assert runner.jobs == []
    assert runner.writes() == []
    assert not (env.hermes_home / "scripts" / SCRIPT_NAME).exists()


def _multiplex_env(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway layout: HERMES_HOME is the multiplex base, not the profile home."""
    monkeypatch.setenv("HERMES_HOME", str(env.profiles.parent))  # <base>/profiles/<profile>


def _fake_hermes_constants(monkeypatch: pytest.MonkeyPatch, override: str | None) -> None:
    from types import ModuleType

    module = ModuleType("hermes_constants")
    module.get_hermes_home_override = lambda: override
    monkeypatch.setitem(sys.modules, "hermes_constants", module)


def test_multiplex_gateway_delivers_with_the_context_override(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.hermes_plugin.pending_reply import resolve_hermes_home

    _multiplex_env(env, monkeypatch)
    # jobs.json lives under the profile home (env.hermes_home), not under the base.
    _fake_hermes_constants(monkeypatch, str(env.hermes_home))
    transform, _critic, _deps = _transform(env, resolve_home=resolve_hermes_home)

    out = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert out != "[SILENT]"
    assert env.status() == "sent"


def test_multiplex_gateway_delivers_when_the_base_home_has_an_env_file(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.hermes_plugin.pending_reply import resolve_hermes_home

    _multiplex_env(env, monkeypatch)
    base_env = env.profiles.parent / ".env"
    base_env.write_text("SOME_KEY=value\n", encoding="utf-8")
    base_env.chmod(0o600)
    _fake_hermes_constants(monkeypatch, str(env.hermes_home))
    transform, _critic, _deps = _transform(env, resolve_home=resolve_hermes_home)

    out = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert out != "[SILENT]"
    assert env.status() == "sent"


def test_critic_raising_delivers_the_safe_replacement_and_marks_sent(env: _Env) -> None:
    transform, critic, _deps = _transform(env)
    critic.review.side_effect = RuntimeError("critic down")

    out = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert out == SAFE_REPLACEMENT
    assert env.status() == "sent"


def test_sanitizer_raising_delivers_the_safe_replacement_and_marks_sent(env: _Env) -> None:
    transform, _critic, dependencies = _transform(env)
    dependencies.sanitize.side_effect = RuntimeError("sanitizer down")

    out = transform(response_text="hi", platform="cron", session_id=SESSION)

    assert out == SAFE_REPLACEMENT
    assert env.status() == "sent"


def test_multiplex_gateway_without_override_stays_silent(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relic.hermes_plugin.pending_reply import resolve_hermes_home

    _multiplex_env(env, monkeypatch)
    # Old behaviour: only the env is read -> <base>/cron/jobs.json has no job -> [SILENT].
    _fake_hermes_constants(monkeypatch, None)
    transform, _critic, _deps = _transform(env, resolve_home=resolve_hermes_home)

    assert transform(response_text="hi", platform="cron", session_id=SESSION) == "[SILENT]"
    assert env.status() == "requested"


# --- job identity from Hermes' cron execution context, and the RELIC_HOME default ---


def _fake_current_execution(monkeypatch: pytest.MonkeyPatch, value) -> None:
    # Public execution identity is a record, not the old gateway tuple.
    execution = None if value is None else SimpleNamespace(
        job_id=value, job_name="Synthetic job", execution_id="ex1",
        source="builtin", scheduled_instant=None, started_at=None, profile="default",
    )
    module = SimpleNamespace(current_cron_execution=lambda: execution)
    monkeypatch.setitem(sys.modules, "cron.execution_identity", module)


def _no_cron_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cron.execution_identity", None)


def test_execution_context_identifies_the_job_when_session_id_does_not(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_current_execution(monkeypatch, JOB_ID)
    transform, _critic, _deps = _transform(env, sanitized="Hi, it's Gumi.")

    out = transform(response_text="Hello", platform="cron", session_id="local-abc")

    assert out == "Hi, it's Gumi."
    assert env.status() == "sent"


def test_execution_context_does_not_leak_into_another_context(
    env: _Env,
) -> None:
    import contextvars
    from relic.hermes_plugin import directory_plugin

    identity = pytest.importorskip("cron.execution_identity")
    token = identity.enter_cron_execution(
        {"id": JOB_ID, "name": "Synthetic first message"}, "ex1", {"source": "builtin"}
    )
    try:
        # CronExecution deliberately has no session_id: the scheduler's
        # ContextVar binding/reset replaces the old tuple's session check.
        assert directory_plugin._cron_job_id("local-abc") == JOB_ID
        transform, _critic, _deps = _transform(env)
        assert contextvars.Context().run(
            transform, response_text="Hello", platform="cron", session_id="local-abc"
        ) == "[SILENT]"
        assert env.status() == "requested"
    finally:
        identity.exit_cron_execution(token)
    assert directory_plugin._cron_job_id("local-abc") is None


def test_execution_context_of_another_job_stays_silent(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.write_jobs([_first_message_job(), {"id": "other02", "name": "something else"}])
    _fake_current_execution(monkeypatch, "other02")
    transform, _critic, _deps = _transform(env)

    assert transform(response_text="Hello", platform="cron", session_id="local-abc") == "[SILENT]"
    assert env.status() == "requested"


def test_without_cron_identity_module_the_session_id_regex_still_delivers(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_cron_identity(monkeypatch)
    transform, _critic, _deps = _transform(env, sanitized="Hi, it's Gumi.")

    out = transform(response_text="Hello", platform="cron", session_id=SESSION)

    assert out == "Hi, it's Gumi."
    assert env.status() == "sent"


def test_no_execution_and_opaque_session_id_stays_silent(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_current_execution(monkeypatch, None)
    transform, _critic, _deps = _transform(env)

    assert transform(response_text="Hello", platform="cron", session_id="local-abc") == "[SILENT]"
    assert env.status() == "requested"


def test_relic_home_none_falls_back_to_the_env(env: _Env) -> None:
    transform, _critic, _deps = _transform(env, sanitized="Hi, it's Gumi.", relic_home=None)

    assert transform(response_text="Hello", platform="cron", session_id=SESSION) == "Hi, it's Gumi."
    assert env.status() == "sent"


def test_relic_home_none_without_env_uses_the_default_home(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RELIC_HOME", raising=False)
    monkeypatch.setattr("relic.paths.get_relic_home", lambda: env.relic_home)
    transform, _critic, _deps = _transform(env, sanitized="Hi, it's Gumi.", relic_home=None)

    assert transform(response_text="Hello", platform="cron", session_id=SESSION) == "Hi, it's Gumi."
    assert env.status() == "sent"
