from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME
from datetime import datetime, timezone

import importlib
import importlib.resources
import importlib.util
import json
import sqlite3
import os
import shutil
import sys
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from hermes_cli.plugins import PluginManager


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.usefixtures("inline_cron_dispatch")
PLUGIN_KEY = "relic-gumi"
EXPECTED_HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "transform_llm_output",
)
REGISTERED_HOOKS = EXPECTED_HOOKS + ("pre_gateway_dispatch",)
SAFE_REPLACEMENT = "I'm here with you. What would feel most helpful right now?"
RUNTIME_SCOPE_ID = "a" * 64



@contextmanager
def _bridge_module():
    module_name = "relic_gumi_bridge_test"
    spec = importlib.util.spec_from_file_location(module_name, REPOSITORY_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


@contextmanager
def _environment(**updates: str | None):
    previous = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _dependencies(*, pcp: str = "pcp", ack: str = "ack"):
    provider = Mock()
    provider.prefetch.return_value = "continuity"
    provider_type = Mock(return_value=provider)
    critic = Mock()
    critic.review.return_value = SimpleNamespace(allow=True)
    observed_scopes: list[str | None] = []

    def inject_context(**_kwargs):
        observed_scopes.append(os.environ.get("RELIC_RUNTIME_SCOPE_ID"))
        return {"context": pcp}

    dependencies = SimpleNamespace(
        memory_provider=provider_type,
        inject_context=Mock(side_effect=inject_context),
        pending_ack_instruction=Mock(return_value=ack),
        resolve_hermes_home=Mock(return_value=Path("/hermes")),
        clear_pending_reply=Mock(),
        output_critic=Mock(return_value=critic),
        sanitize=Mock(return_value="sanitized"),
    )
    return dependencies, provider, critic, observed_scopes


def _registered_hooks(bridge, dependencies, *, relic_home: str | None = None):
    ctx = SimpleNamespace(register_hook=Mock())
    bridge._register_hooks(
        ctx,
        runtime_scope_id="runtime-scope",
        profile_name=HERMES_USER_PROFILE_NAME,
        relic_home=relic_home,
        dependencies=dependencies,
    )
    return ctx, {call.args[0]: call.args[1] for call in ctx.register_hook.call_args_list}


def _write_cron_reservation(
    tmp_path: Path, *, kind: str, job_name: str, job_id: str
) -> tuple[Path, Path]:
    hermes_home = tmp_path / "hermes"
    state_path = hermes_home / "state" / "checkin_guard.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "deliveries": [],
                "pending_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "pending_job_id": job_id,
            }
        ),
        encoding="utf-8",
    )
    relic_home = tmp_path / "relic"
    manifest = relic_home / "user" / "managed_cron.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": {job_name: {"id": job_id, "fingerprint": "test"}},
            }
        ),
        encoding="utf-8",
    )
    return hermes_home, relic_home


def test_registers_explicit_continuity_candidate_tool():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        ctx = SimpleNamespace(register_hook=Mock(), register_tool=Mock())
        bridge._register_hooks(
            ctx,
            runtime_scope_id="runtime-scope",
            profile_name=HERMES_USER_PROFILE_NAME,
            relic_home=None,
            dependencies=dependencies,
        )

    tool_call = ctx.register_tool.call_args.kwargs
    assert tool_call["name"] == "relic_continuity_candidate"
    result = tool_call["handler"]({"marker_id": "candidate-1", "action": "confirm"})
    assert result == {"status": "confirmed", "marker_id": "candidate-1"}
    provider.confirm_candidate.assert_called_once_with("candidate-1")


def test_registers_scoped_maintenance_tools_and_rejects_other_paths(tmp_path):
    with _environment(HERMES_HOME=str(tmp_path)):
        workspace = tmp_path / "workspace" / "gumi"
        workspace.mkdir(parents=True)
        (workspace / "world.md").write_text("world", encoding="utf-8")

        with _bridge_module() as bridge:
            dependencies, _provider, _critic, _observed_scopes = _dependencies()
            ctx = SimpleNamespace(register_hook=Mock(), register_tool=Mock())
            bridge._register_hooks(
                ctx,
                runtime_scope_id="runtime-scope",
                profile_name=HERMES_USER_PROFILE_NAME,
                relic_home=None,
                dependencies=dependencies,
            )

        tools = {call.kwargs["name"]: call.kwargs for call in ctx.register_tool.call_args_list}
        assert {"gumi_read_canon", "gumi_write_report"}.issubset(tools)

        read_canon = tools["gumi_read_canon"]["handler"]
        assert read_canon({"file": "world.md"}) == {
            "status": "ok",
            "file": "world.md",
            "content": "world",
        }
        with pytest.raises(ValueError, match="canonical Gumi file"):
            read_canon({"file": "../USER.md"})

        write_report = tools["gumi_write_report"]["handler"]
        report = json.dumps({"status": "ok"})
        result = write_report({"report": "state_compaction", "content": report})
        assert result == {"status": "ok", "report": "state_compaction"}
        assert (
            tmp_path / "workspace" / "gumi" / "cron" / "world_state_compaction_report.json"
        ).read_text(encoding="utf-8") == report
        with pytest.raises(ValueError, match="maintenance report"):
            write_report({"report": "../../USER.md", "content": report})


def test_pre_hook_presents_pending_candidates_as_explicit_review():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        provider.pending_candidates.return_value = [
            {
                "marker_id": "candidate-1",
                "wording": "Preferisco mantenere il piano attuale",
            }
        ]
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    result = hooks["pre_llm_call"](
        user_message="continuiamo",
        execution_context="foreground",
        subject_visible=True,
    )

    context = result["context"]
    assert "[RELIC CONTINUITY CANDIDATES — NOT CONFIRMED]" in context
    assert "candidate-1" in context
    assert "Preferisco mantenere il piano attuale" in context
    assert "explicit user confirmation" in context


def test_relic_calls_bind_runtime_scope_environment_and_restore_it():
    with _environment(
        RELIC_RUNTIME_SCOPE_ID="outside-scope",
        RELIC_SUBJECT_ID="outside-legacy-scope",
        RELIC_HOME="/outside-home",
    ):
        with _bridge_module() as bridge:
            dependencies, provider, _critic, _observed_scopes = _dependencies()
            observed: list[tuple[str | None, str | None]] = []

            def observe(value: str | None) -> str:
                observed.append(
                    (os.environ.get("RELIC_RUNTIME_SCOPE_ID"), os.environ.get("RELIC_HOME"))
                )
                return value or ""

            provider.prefetch.side_effect = lambda _message: observe("continuity")
            provider.sync_turn.side_effect = lambda *_args: observe(None)
            dependencies.inject_context.side_effect = lambda **_kwargs: {"context": observe("pcp")}
            _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home="/configured-home")

        metadata = {"execution_context": "foreground", "user_visible": True}
        hooks["pre_llm_call"](user_message="hello", **metadata)
        assert os.environ["RELIC_RUNTIME_SCOPE_ID"] == "outside-scope"
        assert os.environ["RELIC_SUBJECT_ID"] == "outside-legacy-scope"
        assert os.environ["RELIC_HOME"] == "/outside-home"

        hooks["post_llm_call"](user_message="hello", assistant_response="welcome", **metadata)
        assert os.environ["RELIC_RUNTIME_SCOPE_ID"] == "outside-scope"
        assert os.environ["RELIC_SUBJECT_ID"] == "outside-legacy-scope"
        assert os.environ["RELIC_HOME"] == "/outside-home"

    assert observed == [
        ("runtime-scope", "/configured-home"),
        ("runtime-scope", "/configured-home"),
        ("runtime-scope", "/configured-home"),
    ]


def test_runtime_scope_environment_restores_values_after_an_exception():
    with _environment(
        RELIC_RUNTIME_SCOPE_ID="outside-scope",
        RELIC_SUBJECT_ID="outside-legacy-scope",
        RELIC_HOME="/outside-home",
    ):
        with _bridge_module() as bridge:
            with pytest.raises(RuntimeError, match="failure"):
                with bridge._runtime_scope_env("runtime-scope", "/configured-home"):
                    raise RuntimeError("failure")

        assert os.environ["RELIC_RUNTIME_SCOPE_ID"] == "outside-scope"
        assert os.environ["RELIC_SUBJECT_ID"] == "outside-legacy-scope"
        assert os.environ["RELIC_HOME"] == "/outside-home"


def test_runtime_scope_environment_isolates_concurrent_scopes():
    with _bridge_module() as bridge:
        first_entered = threading.Event()
        second_attempted = threading.Event()
        observed: list[tuple[str | None, str | None]] = []

        def first() -> None:
            with bridge._runtime_scope_env("first-scope", "/first-home"):
                first_entered.set()
                assert second_attempted.wait(timeout=1)
                observed.append(
                    (os.environ.get("RELIC_RUNTIME_SCOPE_ID"), os.environ.get("RELIC_HOME"))
                )

        def second() -> None:
            assert first_entered.wait(timeout=1)
            second_attempted.set()
            with bridge._runtime_scope_env("second-scope", "/second-home"):
                observed.append(
                    (os.environ.get("RELIC_RUNTIME_SCOPE_ID"), os.environ.get("RELIC_HOME"))
                )

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()

    assert observed == [
        ("first-scope", "/first-home"),
        ("second-scope", "/second-home"),
    ]


def test_registered_hooks_call_real_relic_seams():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, observed_scopes = _dependencies()
        ctx, hooks = _registered_hooks(bridge, dependencies)

    assert tuple(hooks) == EXPECTED_HOOKS
    dependencies.memory_provider.assert_called_once_with(
        runtime_scope_id="runtime-scope", hermes_profile_id=HERMES_USER_PROFILE_NAME, relic_home=None
    )

    result = hooks["pre_llm_call"](
        execution_context="foreground",
        user_visible=True,
        session_id="session-1",
        user_message="hello",
        platform="telegram",
    )

    assert result == {"context": "continuity\n\npcp\n\nack"}
    assert observed_scopes == ["runtime-scope"]
    dependencies.inject_context.assert_called_once_with(
        execution_context="foreground",
        user_visible=True,
        session_id="session-1",
        user_message="hello",
        platform="telegram",
    )
    provider.prefetch.assert_called_once_with("hello")
    dependencies.pending_ack_instruction.assert_called_once_with(Path("/hermes"))

    hooks["post_llm_call"](
        execution_context="foreground",
        user_visible=True,
        user_message="hello",
        assistant_response="welcome",
    )

    dependencies.clear_pending_reply.assert_called_once_with(Path("/hermes"))
    provider.sync_turn.assert_called_once_with("hello", "welcome")
    assert dependencies.resolve_hermes_home.call_count == 2


@pytest.mark.parametrize(
    ("action", "provider_method"),
    (("confirm", "confirm_candidate"), ("reject", "reject_candidate")),
)
def test_post_hook_routes_explicit_candidate_action(action, provider_method):
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    hooks["post_llm_call"](
        execution_context="foreground",
        user_visible=True,
        user_message="confirmation prose must not matter",
        assistant_response="welcome",
        continuity_candidate_action={"marker_id": "candidate-1", "action": action},
    )

    getattr(provider, provider_method).assert_called_once_with("candidate-1")
    getattr(
        provider,
        "reject_candidate" if provider_method == "confirm_candidate" else "confirm_candidate",
    ).assert_not_called()


def test_post_hook_does_not_infer_candidate_action_from_user_prose():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    hooks["post_llm_call"](
        execution_context="foreground",
        user_visible=True,
        user_message="I confirm candidate-1",
        assistant_response="welcome",
    )

    provider.confirm_candidate.assert_not_called()
    provider.reject_candidate.assert_not_called()


def test_post_hook_ignores_candidate_action_failure():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)
    provider.confirm_candidate.side_effect = RuntimeError("store unavailable")

    assert (
        hooks["post_llm_call"](
            execution_context="foreground",
            user_visible=True,
            user_message="confirm",
            assistant_response="welcome",
            continuity_candidate_action={"marker_id": "candidate-1", "action": "confirm"},
        )
        is None
    )

    provider.sync_turn.assert_called_once_with("confirm", "welcome")
def test_post_hook_captures_user_reply_when_assistant_is_empty(monkeypatch):
    captured: list[tuple[Path, dict[str, object]]] = []

    def record_reply(hermes_home: Path, **kwargs: object) -> None:
        captured.append((hermes_home, kwargs))

    monkeypatch.setattr("relic.profile.user_runtime.record_subject_reply", record_reply)
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(
            bridge, dependencies, relic_home="/canonical-relic"
        )

    hooks["post_llm_call"](
        execution_context="foreground",
        user_visible=True,
        user_message="reply to the check-in",
        assistant_response="",
    )

    provider.sync_turn.assert_called_once_with("reply to the check-in", "")
    dependencies.clear_pending_reply.assert_not_called()
    assert captured == [
        (
            Path("/hermes"),
            {
                "relic_home": Path("/canonical-relic"),
                "runtime_scope_id": "runtime-scope",
            },
        )
    ]



@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"execution_context": "background", "user_visible": True},
        {"execution_context": "foreground", "user_visible": False},
        {"execution_context": "foreground", "user_visible": 1},
    ),
)
def test_callbacks_fail_closed_without_foreground_user_metadata(metadata):
    with _bridge_module() as bridge:
        dependencies, provider, critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    assert hooks["pre_llm_call"](session_id="session-1", user_message="hello", **metadata) is None
    assert (
        hooks["post_llm_call"](
            user_message="hello", assistant_response="welcome", **metadata
        )
        is None
    )
    assert hooks["transform_llm_output"](response_text="visible", **metadata) is None
    provider.prefetch.assert_not_called()
    provider.sync_turn.assert_not_called()
    dependencies.inject_context.assert_not_called()
    dependencies.pending_ack_instruction.assert_not_called()
    dependencies.clear_pending_reply.assert_not_called()
    critic.review.assert_not_called()
    dependencies.sanitize.assert_not_called()


def test_transform_replaces_rejected_output_and_returns_none_only_when_unchanged():
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    transform = hooks["transform_llm_output"]
    metadata = {"execution_context": "foreground", "user_visible": True}

    dependencies.sanitize.return_value = "rewritten"
    assert transform(response_text="raw", **metadata) == "rewritten"
    critic.review.assert_called_with("raw", consensual=True)

    dependencies.sanitize.return_value = "raw"
    assert transform(response_text="raw", **metadata) is None

    dependencies.sanitize.return_value = None
    assert transform(response_text="operator-only", **metadata) == SAFE_REPLACEMENT

    critic.review.return_value = SimpleNamespace(allow=False)
    assert transform(response_text="unsafe", **metadata) == SAFE_REPLACEMENT
    critic.review.side_effect = RuntimeError("critic unavailable")
    assert transform(response_text="must not escape", **metadata) == SAFE_REPLACEMENT

    critic.review.side_effect = None
    critic.review.return_value = SimpleNamespace(allow=True)
    dependencies.sanitize.side_effect = RuntimeError("sanitizer unavailable")
    assert transform(response_text="must not escape", **metadata) == SAFE_REPLACEMENT

def test_cron_output_is_silenced_without_a_relic_delivery_reservation(tmp_path: Path):
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = tmp_path / "hermes"
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    assert (
        hooks["transform_llm_output"](
            response_text="rogue cron output",
            platform="cron",
            session_id="cron-job-1",
        )
        == "[SILENT]"
    )
    critic.review.assert_not_called()


def test_cron_maintenance_output_is_finalized_without_delivery_reservation(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    workspace = hermes_home / "workspace" / "gumi"
    workspace.mkdir(parents=True)
    for name in (
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    ):
        (workspace / name).write_text(name, encoding="utf-8")
    job_id = "a" * 12
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": job_id, "name": "gumi_state_compaction"}]}),
        encoding="utf-8",
    )

    from relic.profile.maintenance import prepare_run

    run = prepare_run(workspace, "state_compaction")
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    result = hooks["transform_llm_output"](
        response_text=json.dumps(
            {"snapshot_id": run["snapshot_id"], "status": "ok", "candidates": []}
        ),
        platform="cron",
        session_id=f"cron_{job_id}_20260821_120000",
    )

    assert result == "[SILENT]"
    assert (workspace / "cron" / "world_state_compaction_report.json").is_file()
    assert (workspace / "cron" / ".state_compaction_baseline.json").is_file()
    critic.review.assert_not_called()


def test_cron_maintenance_invalid_output_stays_visible_and_does_not_advance_baseline(
    tmp_path: Path,
):
    hermes_home = tmp_path / "hermes"
    workspace = hermes_home / "workspace" / "gumi"
    workspace.mkdir(parents=True)
    for name in (
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    ):
        (workspace / name).write_text(name, encoding="utf-8")
    job_id = "b" * 12
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": job_id, "name": "gumi_candidate_review"}]}),
        encoding="utf-8",
    )

    from relic.profile.maintenance import prepare_run

    prepare_run(workspace, "candidate_review")
    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    result = hooks["transform_llm_output"](
        response_text="not-json",
        platform="cron",
        session_id=f"cron_{job_id}_20260821_120000",
    )

    assert result.startswith("[maintenance finalizer failed:")
    assert not (workspace / "cron" / ".candidate_review_baseline.json").exists()


def test_cron_pending_maintenance_does_not_capture_unmapped_session(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    workspace = hermes_home / "workspace" / "gumi"
    workspace.mkdir(parents=True)
    for name in (
        "world.md",
        "voice_canon.json",
        "visual_canon.json",
        "lyria_canon.json",
        "media_policy.json",
        "relationship_policy.md",
    ):
        (workspace / name).write_text(name, encoding="utf-8")
    job_id = "c" * 12
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"job_id": job_id, "name": "gumi_state_compaction"}]}),
        encoding="utf-8",
    )

    from relic.profile.maintenance import prepare_run

    run = prepare_run(workspace, "state_compaction")
    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    result = hooks["transform_llm_output"](
        response_text="normal scheduled response",
        platform="cron",
        session_id="cron_unrelated_20260821_120000",
    )

    assert result == "[SILENT]"
    assert not (workspace / "cron" / ".state_compaction_baseline.json").exists()
    assert (workspace / "cron" / ".state_compaction_pending.json").is_file()


def test_cron_output_requires_and_consumes_one_delivery_reservation(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    state_path = hermes_home / "state" / "checkin_guard.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"deliveries": [], "pending_at": "'
        + datetime.now(timezone.utc).isoformat()
        + '"}\n',
        encoding="utf-8",
    )
    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    assert (
        hooks["transform_llm_output"](
            response_text="approved cron output", platform="cron", session_id="cron-job-1"
        )
        == "[SILENT]"
    )
    assert (
        hooks["transform_llm_output"](
            response_text="second cron output", platform="cron", session_id="cron-job-2"
        )
        == "[SILENT]"
    )


def test_cron_dispatch_reservation_calls_checkin_media_dispatcher_and_stays_silent(
    tmp_path: Path,
):
    hermes_home, relic_home = _write_cron_reservation(
        tmp_path,
        kind="checkin",
        job_name="gumi_checkin_message",
        job_id="checkinjob01",
    )
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch(
        "relic.gumi_plugin.checkin_media_dispatcher.dispatch",
        return_value={"tipo": "text", "success": True},
    ) as dispatch_mock:
        result = hooks["transform_llm_output"](
            response_text="approved cron output",
            platform="cron",
            session_id="cron_checkinjob01_20260101_120000",
        )

    assert result == "[SILENT]"
    dispatch_mock.assert_called_once_with(
        "approved cron output",
        hermes_home,
        relic_home / "user",
        continuity_kind="checkin",
    )
    critic.review.assert_not_called()


def test_failed_checkin_dispatch_discards_unconfirmed_exchange(tmp_path: Path):
    hermes_home, relic_home = _write_cron_reservation(
        tmp_path,
        kind="checkin",
        job_name="gumi_checkin_message",
        job_id="checkinjob02",
    )
    db_path = relic_home / "user" / "checkin" / "relic.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE checkin_exchanges (
                id INTEGER PRIMARY KEY,
                facet_id TEXT,
                question_text TEXT NOT NULL,
                reply_text TEXT,
                asked_at TEXT NOT NULL
            );
            CREATE TABLE checkin_features (
                id INTEGER PRIMARY KEY,
                tick_id TEXT NOT NULL,
                posture TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO checkin_exchanges VALUES (1, 'facet', 'question', NULL, ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO checkin_features VALUES (1, ?, 'ask', ?)",
            (now, now),
        )

    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch(
        "relic.gumi_plugin.checkin_media_dispatcher.dispatch",
        return_value={"tipo": "text", "success": False},
    ):
        result = hooks["transform_llm_output"](
            response_text="approved cron output",
            platform="cron",
            session_id="cron_checkinjob02_20260101_120000",
        )

    assert result == "[SILENT]"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM checkin_exchanges").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM checkin_features").fetchone() == (0,)


def test_cron_dispatch_reservation_with_diegetic_kind_calls_dispatcher_with_diegetic_kind(
    tmp_path: Path,
):
    hermes_home, relic_home = _write_cron_reservation(
        tmp_path,
        kind="diegetic",
        job_name="gumi_diegetic_message",
        job_id="diegetic001",
    )
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch(
        "relic.gumi_plugin.checkin_media_dispatcher.dispatch",
        return_value={"tipo": "text", "success": True},
    ) as dispatch_mock:
        result = hooks["transform_llm_output"](
            response_text="approved cron output",
            platform="cron",
            session_id="cron_diegetic001_20260101_120000",
        )

    assert result == "[SILENT]"
    dispatch_mock.assert_called_once_with(
        "approved cron output",
        hermes_home,
        relic_home / "user",
        continuity_kind="diegetic",
    )
    critic.review.assert_not_called()


def test_cron_dispatch_reservation_with_diegetic_kind_appends_missing_question_mark(
    tmp_path: Path,
):
    hermes_home, relic_home = _write_cron_reservation(
        tmp_path,
        kind="diegetic",
        job_name="gumi_diegetic_message",
        job_id="diegetic002",
    )
    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch(
        "relic.gumi_plugin.checkin_media_dispatcher.dispatch",
        return_value={"tipo": "text", "success": True},
    ) as dispatch_mock:
        result = hooks["transform_llm_output"](
            response_text=(
                "Tu che genere di suoni ascolti quando è un momento così tranquillo ✨"
            ),
            platform="cron",
            session_id="cron_diegetic002_20260101_120000",
        )

    assert result == "[SILENT]"
    dispatch_mock.assert_called_once_with(
        "Tu che genere di suoni ascolti quando è un momento così tranquillo? ✨",
        hermes_home,
        relic_home / "user",
        continuity_kind="diegetic",
    )


def test_cron_dispatch_exception_is_swallowed_and_still_silent(tmp_path: Path):
    hermes_home, relic_home = _write_cron_reservation(
        tmp_path,
        kind="proactivity",
        job_name="gumi_proactive_message",
        job_id="proactjob001",
    )
    with _bridge_module() as bridge:
        dependencies, _provider, _critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch(
        "relic.gumi_plugin.checkin_media_dispatcher.dispatch",
        side_effect=RuntimeError("dispatch exploded"),
    ) as dispatch_mock:
        result = hooks["transform_llm_output"](
            response_text="approved cron output",
            platform="cron",
            session_id="cron_proactjob001_20260101_120000",
        )

    assert result == "[SILENT]"
    dispatch_mock.assert_called_once()


def test_cron_output_without_reservation_never_calls_dispatch(tmp_path: Path):
    hermes_home = tmp_path / "hermes"
    relic_home = tmp_path / "relic"
    with _bridge_module() as bridge:
        dependencies, _provider, critic, _observed_scopes = _dependencies()
        dependencies.resolve_hermes_home.return_value = hermes_home
        _ctx, hooks = _registered_hooks(bridge, dependencies, relic_home=str(relic_home))

    with patch("relic.gumi_plugin.checkin_media_dispatcher.dispatch") as dispatch_mock:
        result = hooks["transform_llm_output"](
            response_text="rogue cron output", platform="cron", session_id="cron-job-1"
        )

    assert result == "[SILENT]"
    dispatch_mock.assert_not_called()
    critic.review.assert_not_called()



def test_canonical_user_runtime_and_host_api_are_required(tmp_path: Path):
    from relic.profile.registry import ProfileRegistry

    relic_home = tmp_path / "relic"
    user = ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=tmp_path / "profiles",
    ).initialize_user()

    with _environment(
        RELIC_HOME=str(relic_home),
        RELIC_RUNTIME_SCOPE_ID=None,
        RELIC_SUBJECT_ID=None,
        HERMES_PROFILE=None,
    ):
        with _bridge_module() as bridge:
            unsupported_ctx = SimpleNamespace()
            with pytest.raises(RuntimeError, match="hook registration"):
                bridge.register(unsupported_ctx)

            dependencies, _provider, _critic, _observed_scopes = _dependencies()
            configured_ctx = SimpleNamespace(register_hook=Mock())
            with patch.object(bridge, "_load_relic_dependencies", return_value=dependencies):
                bridge.register(configured_ctx)

    dependencies.memory_provider.assert_called_once_with(
        runtime_scope_id=user.runtime_scope_id,
        hermes_profile_id=HERMES_USER_PROFILE_NAME,
        relic_home=str(relic_home),
    )
    assert {call.args[0] for call in configured_ctx.register_hook.call_args_list} == set(
        REGISTERED_HOOKS
    )


def test_plugin_without_user_profile_registers_no_continuity_hooks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A fresh install has no User profile yet: register() must not raise (catalog
    validation probes it that way), must say why, and must leave the continuity hooks off."""
    with _environment(RELIC_HOME=str(tmp_path / "missing-relic")):
        with _bridge_module() as bridge:
            ctx = SimpleNamespace(register_hook=Mock())

            with caplog.at_level("WARNING"):
                bridge.register(ctx)

    registered = {call.args[0] for call in ctx.register_hook.call_args_list}
    assert registered <= {"pre_gateway_dispatch"}
    assert not registered & {"pre_llm_call", "post_llm_call", "transform_llm_output"}
    assert (
        "relic-gumi requires canonical User profile at "
        f"{tmp_path / 'missing-relic/user/user_profile.json'}"
    ) in caplog.text


def _unload_plugin_modules() -> None:
    prefix = "hermes_plugins.relic_gumi"
    for name in tuple(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            sys.modules.pop(name)


def _write_config(hermes_home: Path, *, enabled: bool) -> None:
    plugins: dict[str, object] = {"enabled": [PLUGIN_KEY] if enabled else []}
    (hermes_home / "config.yaml").write_text(
        json.dumps({"plugins": plugins}), encoding="utf-8"
    )


def _write_inert_plugin(plugin_dir: Path) -> None:
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: relic-gumi\nkind: standalone\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n    pass\n", encoding="utf-8"
    )


@pytest.fixture
def installed_plugin(tmp_path: Path):
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / PLUGIN_KEY
    shutil.copytree(
        REPOSITORY_ROOT,
        plugin_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    bundled_plugins = tmp_path / "empty-bundled-plugins"
    bundled_plugins.mkdir()

    with _environment(
        HERMES_HOME=str(hermes_home),
        HERMES_BUNDLED_PLUGINS=str(bundled_plugins),
        HERMES_ENABLE_PROJECT_PLUGINS=None,
        RELIC_HOME=None,
        RELIC_RUNTIME_SCOPE_ID=None,
        RELIC_SUBJECT_ID=None,
        HERMES_PROFILE=None,
    ):
        _unload_plugin_modules()
        yield hermes_home
        _unload_plugin_modules()


def _discover(hermes_home: Path) -> PluginManager:
    manager = PluginManager()
    manager.discover_and_load()
    assert PLUGIN_KEY in manager._plugins, (
        f"{PLUGIN_KEY} was not discovered from "
        f"{hermes_home / 'plugins' / PLUGIN_KEY}; directory plugins require a root plugin.yaml."
    )
    return manager


def test_real_loader_registers_three_hooks_for_the_canonical_user(installed_plugin: Path):
    from relic.profile.registry import ProfileRegistry

    relic_home = installed_plugin / "relic"
    ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=installed_plugin / "profiles",
    ).initialize_user()

    with _environment(RELIC_HOME=str(relic_home)):
        _write_config(installed_plugin, enabled=True)
        manager = _discover(installed_plugin)
        loaded = manager._plugins[PLUGIN_KEY]

    assert loaded.enabled is True, loaded.error
    assert loaded.error is None
    assert set(manager._hooks) == set(REGISTERED_HOOKS)
    assert set(loaded.hooks_registered) == set(REGISTERED_HOOKS)
    assert all(len(manager._hooks[name]) == 1 for name in REGISTERED_HOOKS)
    assert manager.invoke_hook(
        "pre_llm_call",
        execution_context="background",
        user_visible=True,
        session_id="session-1",
        user_message="must not persist",
    ) == []


def test_verifier_uses_the_profile_plugin_with_real_hermes_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from relic.hermes_plugin.install import verify_installed_plugin
    from relic.profile.registry import ProfileRegistry

    base_home = tmp_path / "hermes"
    profile_home = base_home / "profiles" / HERMES_USER_PROFILE_NAME
    relic_home = tmp_path / "relic"
    ProfileRegistry(
        relic_home=relic_home, hermes_profiles_home=base_home / "profiles"
    ).initialize_user()
    plugin_dir = profile_home / "plugins" / PLUGIN_KEY
    shutil.copytree(
        REPOSITORY_ROOT,
        plugin_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    _write_config(profile_home, enabled=True)
    _write_config(base_home, enabled=True)
    _write_inert_plugin(base_home / "plugins" / PLUGIN_KEY)
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_inert_plugin(project_root / ".hermes" / "plugins" / PLUGIN_KEY)
    bundled_plugins = tmp_path / "empty-bundled-plugins"
    bundled_plugins.mkdir()
    outside_home = tmp_path / "outside-hermes"
    callback_calls: list[str] = []

    def profile(frame, event, _arg):
        if (
            event == "call"
            and frame.f_globals.get("__name__") == "relic.hermes_plugin.directory_plugin"
            and frame.f_code.co_name in EXPECTED_HOOKS
        ):
            callback_calls.append(frame.f_code.co_name)
        return profile

    monkeypatch.chdir(project_root)
    with _environment(
        HERMES_HOME=str(outside_home),
        HERMES_PROFILES_HOME=str(base_home / "profiles"),
        HERMES_BUNDLED_PLUGINS=str(bundled_plugins),
        HERMES_ENABLE_PROJECT_PLUGINS="1",
        RELIC_HOME=str(relic_home),
        RELIC_RUNTIME_SCOPE_ID=None,
        RELIC_SUBJECT_ID=None,
        HERMES_PROFILE=None,
    ):
        _unload_plugin_modules()
        previous_profile = sys.getprofile()
        sys.setprofile(profile)
        try:
            verify_installed_plugin(profile_home)
        finally:
            sys.setprofile(previous_profile)

        assert not (profile_home / "state" / "checkin_guard.json").exists()

        assert os.environ["HERMES_HOME"] == str(outside_home)
        assert os.environ["HERMES_PROFILES_HOME"] == str(base_home / "profiles")
        assert os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] == "1"
        assert os.environ["RELIC_HOME"] == str(relic_home)

        with _environment(
            HERMES_HOME=str(profile_home),
            HERMES_ENABLE_PROJECT_PLUGINS=None,
        ):
            _unload_plugin_modules()
            manager = _discover(profile_home)
            loaded = manager._plugins[PLUGIN_KEY]
            assert Path(loaded.module.__file__).resolve() == plugin_dir / "__init__.py"
            assert set(loaded.hooks_registered) == set(REGISTERED_HOOKS)
        _unload_plugin_modules()

    assert callback_calls == list(EXPECTED_HOOKS)

def test_real_loader_loads_without_user_profile_but_without_continuity_hooks(installed_plugin: Path):
    with _environment(RELIC_HOME=str(installed_plugin / "missing-relic")):
        _write_config(installed_plugin, enabled=True)
        manager = _discover(installed_plugin)
        loaded = manager._plugins[PLUGIN_KEY]

    assert loaded.error is None
    assert not set(loaded.hooks_registered) & {
        "pre_llm_call",
        "post_llm_call",
        "transform_llm_output",
    }


def test_installed_relic_package_includes_runtime_data():
    relic = importlib.import_module("relic")
    package = importlib.resources.files(relic)

    assert package.joinpath("db/migrations/0001_initial.sql").is_file()
    from relic.context_pack.schema import SCHEMA_PATH

    assert SCHEMA_PATH.is_file()


def test_release_source_runtime_paths_are_tracked_and_not_ignored():
    if not (REPOSITORY_ROOT / ".git").is_dir() or shutil.which("git") is None:
        pytest.skip("requires a Git checkout")

    expected = {
        "src/relic/artifacts/__init__.py",
        "src/relic/artifacts/checksums.py",
        "src/relic/artifacts/types.py",
    }
    tracked = set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *sorted(expected)],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert expected <= tracked
    assert ignored.returncode == 1, ignored.stdout


def test_chronicle_runtime_scope_purge_database_failure_is_explicit(tmp_path: Path):
    from relic.chronicle import retention

    with (
        patch("relic.chronicle.emitter._require_runtime_scope_schema"),
        patch.object(retention, "_get_db_connection", side_effect=OSError("db unavailable")),
        pytest.raises(RuntimeError, match="chronicle database purge failed"),
    ):
        retention.purge_runtime_scope_records(
            RUNTIME_SCOPE_ID, relic_home=tmp_path, cascade=True
        )


def test_runtime_scope_purge_rejects_legacy_subject_schema(tmp_path: Path):
    from relic.chronicle import emitter, retention
    from relic.chronicle.emitter import LegacyChronicleMigrationRequired

    db_path = tmp_path / "relic.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE chronicle_events (event_id TEXT PRIMARY KEY, subject_id TEXT)"
    )
    conn.close()

    with (
        patch.object(emitter, "_get_db_connection", lambda: sqlite3.connect(db_path)),
        pytest.raises(LegacyChronicleMigrationRequired, match="export.*remove"),
    ):
        retention.purge_runtime_scope_records(
            RUNTIME_SCOPE_ID, relic_home=tmp_path, cascade=True
        )


def test_legacy_subject_purge_deletes_subject_rows_and_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from relic.chronicle import retention

    db_path = tmp_path / "relic.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE chronicle_events (
            event_id TEXT PRIMARY KEY, subject_id TEXT, trace_id TEXT
        );
        CREATE TABLE chronicle_decisions (
            decision_id TEXT PRIMARY KEY, subject_id TEXT, trace_id TEXT
        );
        CREATE TABLE chronicle_state_snapshots (
            snapshot_id TEXT PRIMARY KEY, subject_id TEXT, trace_id TEXT
        );
        CREATE TABLE chronicle_provenance_edges (
            edge_id TEXT PRIMARY KEY, from_node_id TEXT, trace_id TEXT
        );
        CREATE TABLE chronicle_access_log (access_id TEXT PRIMARY KEY, target_filter TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO chronicle_events VALUES (?, ?, ?)",
        [
            ("target-event", "subject", "target-trace"),
            ("other-event", "other", "other-trace"),
        ],
    )
    conn.executemany(
        "INSERT INTO chronicle_decisions VALUES (?, ?, ?)",
        [
            ("target-decision", "subject", "target-trace"),
            ("other-decision", "other", "other-trace"),
        ],
    )
    conn.executemany(
        "INSERT INTO chronicle_state_snapshots VALUES (?, ?, ?)",
        [
            ("target-snapshot", "subject", "target-trace"),
            ("other-snapshot", "other", "other-trace"),
        ],
    )
    conn.executemany(
        "INSERT INTO chronicle_provenance_edges VALUES (?, ?, ?)",
        [
            ("target-edge", "target-event", "target-trace"),
            ("target-trace-edge", "artifact", "target-trace"),
            ("other-edge", "other-event", "other-trace"),
        ],
    )
    conn.executemany(
        "INSERT INTO chronicle_access_log VALUES (?, ?)",
        [
            ("target-access", '{"subject_id": "subject"}'),
            ("other-access", '{"subject_id": "other"}'),
        ],
    )
    conn.commit()
    conn.close()

    (tmp_path / "decision_events.jsonl").write_text(
        '{"subject_id": "subject"}\n{"subject_id": "other"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(retention, "_get_db_connection", lambda: sqlite3.connect(db_path))

    result = retention.purge_legacy_subject_records(
        "subject", relic_home=tmp_path, cascade=True
    )
    assert result["chronicle_events_deleted"] == 1
    assert result["chronicle_decisions_deleted"] == 1
    assert result["chronicle_state_snapshots_deleted"] == 1
    assert result["chronicle_provenance_edges_deleted"] == 2
    assert result["chronicle_access_log_deleted"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT * FROM chronicle_events").fetchall() == [
            ("other-event", "other", "other-trace")
        ]
        assert conn.execute("SELECT * FROM chronicle_decisions").fetchall() == [
            ("other-decision", "other", "other-trace")
        ]
        assert conn.execute("SELECT * FROM chronicle_state_snapshots").fetchall() == [
            ("other-snapshot", "other", "other-trace")
        ]
        assert conn.execute("SELECT * FROM chronicle_provenance_edges").fetchall() == [
            ("other-edge", "other-event", "other-trace")
        ]
        assert conn.execute("SELECT * FROM chronicle_access_log").fetchall() == [
            ("other-access", '{"subject_id": "other"}')
        ]
    finally:
        conn.close()
    assert result["legacy_jsonl_lines_removed"] == 1
    assert (tmp_path / "decision_events.jsonl").read_text(encoding="utf-8") == (
        '{"subject_id": "other"}\n'
    )


def test_continuity_erasure_clears_and_evicts_live_durable_service():
    from relic.shared_continuity import service as continuity

    fallback = Mock()
    durable = Mock()
    continuity._durable_services["subject"] = durable
    try:
        with patch.object(continuity, "_service", fallback):
            result = continuity.forget_continuity_subject("subject")

        fallback.forget_subject.assert_called_once_with("subject")
        durable.forget_subject.assert_called_once_with("subject")
        assert "subject" not in continuity._durable_services
        assert "subject" in continuity._erased_subjects
        assert result["services_cleared"] == 2

        continuity.reactivate_continuity_subject("subject")
        assert "subject" not in continuity._erased_subjects
    finally:
        continuity._durable_services.pop("subject", None)
        continuity._erased_subjects.discard("subject")


def test_checkin_reply_turn_injects_followup_and_records_chain_step(tmp_path):
    from relic.checkin.db_init import init_db, seed_facets
    from relic.checkin.followup_chain import INSTRUCTION
    from relic.checkin.reply_capture import capture_reply_if_pending

    checkin_dir = tmp_path / "relic" / "user" / "checkin"
    checkin_dir.mkdir(parents=True)
    db = checkin_dir / "relic.db"
    policy = checkin_dir / "delivery_policy.json"
    policy.write_text(json.dumps({"consent_for_active_elicitation": True}))
    conn = init_db(db)
    seed_facets(conn)
    conn.execute(
        "INSERT INTO checkin_exchanges (facet_id, question_text, asked_at) "
        "VALUES ('aesthetic.music_taste', 'Che musica ascolti?', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    with _bridge_module() as bridge:
        dependencies, provider, _critic, _observed_scopes = _dependencies()
        provider.sync_turn.side_effect = lambda user_msg, _assistant: capture_reply_if_pending(
            user_msg, "user", db_path=db, delivery_policy_path=policy
        )
        _ctx, hooks = _registered_hooks(
            bridge, dependencies, relic_home=str(tmp_path / "relic")
        )
        turn = dict(
            execution_context="foreground",
            user_visible=True,
            session_id="session-1",
            user_message="Un disco jazz di Miles Davis, ieri sera",
            platform="telegram",
        )
        result = hooks["pre_llm_call"](**turn)
        hooks["post_llm_call"](**turn, assistant_response="Che bello! Dove lo ascoltavi?")

    assert result["context"].endswith(INSTRUCTION)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT facet_id, question_text, reply_text, chain_depth "
            "FROM checkin_exchanges ORDER BY id"
        ).fetchall()
    assert rows[0][2] == "Un disco jazz di Miles Davis, ieri sera"
    assert rows[1] == ("aesthetic.music_taste", "Dove lo ascoltavi?", None, 1)


# --- stock Hermes: turn ownership derived from the arguments every hook receives ---


def _stock_turn(**overrides):
    base = {
        "session_id": "s-1",
        "turn_id": "t-1",
        "platform": "cli",
        "parent_session_id": "",
        "sender_id": "",
    }
    base.update(overrides)
    return base


def test_stock_user_turn_gets_context_records_and_sanitizes():
    with _bridge_module() as bridge:
        dependencies, provider, critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    turn = _stock_turn()
    assert hooks["pre_llm_call"](user_message="ciao", **turn) is not None
    provider.prefetch.assert_called_once()
    hooks["post_llm_call"](user_message="ciao", assistant_response="welcome", **turn)
    provider.sync_turn.assert_called_once()
    hooks["transform_llm_output"](response_text="visible", **turn)
    critic.review.assert_called_once()


@pytest.mark.parametrize("platform", ["cron", "subagent", "curator", "gateway_hygiene", "CRON"])
def test_stock_internal_platforms_are_ignored(platform):
    with _bridge_module() as bridge:
        dependencies, provider, critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    turn = _stock_turn(platform=platform)
    assert hooks["pre_llm_call"](user_message="x", **turn) is None
    assert hooks["post_llm_call"](user_message="x", assistant_response="y", **turn) is None
    if platform.lower() != "cron":  # cron has its own delivery path in transform
        assert hooks["transform_llm_output"](response_text="z", **turn) is None
    provider.prefetch.assert_not_called()
    provider.sync_turn.assert_not_called()
    critic.review.assert_not_called()


def test_stock_delegated_child_with_parent_session_is_ignored():
    with _bridge_module() as bridge:
        dependencies, provider, critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    turn = _stock_turn(parent_session_id="s-0")
    assert hooks["pre_llm_call"](user_message="x", **turn) is None
    assert hooks["transform_llm_output"](response_text="z", **turn) is None
    provider.prefetch.assert_not_called()
    critic.review.assert_not_called()


def test_stock_review_fork_is_ignored_because_it_never_passed_pre_llm_call():
    """Hermes skips pre/post_llm_call for the memory-review fork but still calls
    transform_llm_output with the parent's session id and platform."""
    with _bridge_module() as bridge:
        dependencies, provider, critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    assert hooks["pre_llm_call"](user_message="hi", **_stock_turn(turn_id="user-turn")) is not None
    fork = _stock_turn(turn_id="fork-turn")  # same session, same platform, other turn
    critic.review.reset_mock()
    assert hooks["transform_llm_output"](response_text="review summary", **fork) is None
    critic.review.assert_not_called()
    hooks["transform_llm_output"](response_text="answer", **_stock_turn(turn_id="user-turn"))
    critic.review.assert_called_once()


def test_stock_turn_without_ids_fails_closed():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    assert hooks["pre_llm_call"](user_message="x", platform="cli") is None
    assert hooks["pre_llm_call"](user_message="x", turn_id="t") is None
    provider.prefetch.assert_not_called()


def test_admitted_turn_ledger_is_bounded():
    from relic.hermes_plugin.directory_plugin import _AdmittedTurns

    ledger = _AdmittedTurns(limit=3)
    for index in range(5):
        ledger.admit(f"t{index}")
    assert "t0" not in ledger and "t1" not in ledger
    assert all(f"t{index}" in ledger for index in (2, 3, 4))
    assert "" not in ledger


def test_host_metadata_still_overrides_derivation():
    with _bridge_module() as bridge:
        dependencies, provider, _critic, _scopes = _dependencies()
        _ctx, hooks = _registered_hooks(bridge, dependencies)

    # A patched host that says "background" wins even if the platform looks like a user.
    turn = _stock_turn(execution_context="background", user_visible=True)
    assert hooks["pre_llm_call"](user_message="x", **turn) is None
    provider.prefetch.assert_not_called()
