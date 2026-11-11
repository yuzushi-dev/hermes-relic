from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _cron_hooks(tmp_path: Path):
    from relic.hermes_plugin import directory_plugin

    provider = Mock()
    provider.prefetch.return_value = ""
    dependencies = SimpleNamespace(
        memory_provider=Mock(return_value=provider),
        inject_context=Mock(return_value={"context": ""}),
        pending_ack_instruction=Mock(return_value=""),
        resolve_hermes_home=Mock(return_value=tmp_path / "hermes"),
        clear_pending_reply=Mock(),
        output_critic=Mock(),
        sanitize=Mock(return_value="safe"),
    )
    context = SimpleNamespace(register_hook=Mock())
    directory_plugin._register_hooks(
        context,
        runtime_scope_id="scope",
        profile_name=HERMES_USER_PROFILE_NAME,
        relic_home=str(tmp_path / "relic"),
        dependencies=dependencies,
    )
    return {call.args[0]: call.args[1] for call in context.register_hook.call_args_list}


def test_media_message_is_dispatched_detached_from_the_hook(tmp_path: Path) -> None:
    hooks = _cron_hooks(tmp_path)
    with (
        patch(
            "relic.profile.user_runtime.consume_delivery_reservation",
            return_value=(True, "proactivity"),
        ),
        patch("relic.gumi_plugin.checkin_media_dispatcher.dispatch") as dispatch,
        patch("subprocess.Popen") as popen,
    ):
        assert hooks["transform_llm_output"](
            response_text="tipo: music\nlo-fi track\nargomento: università",
            platform="cron",
            session_id="cron_job_20260101_120000",
        ) == "[SILENT]"
    dispatch.assert_not_called()
    popen.assert_called_once()
    assert popen.call_args.kwargs["start_new_session"] is True
    assert (tmp_path / "hermes" / "logs" / "cron-dispatch.log").exists()


def test_text_message_is_dispatched_detached_too(tmp_path: Path) -> None:
    hooks = _cron_hooks(tmp_path)
    with (
        patch(
            "relic.profile.user_runtime.consume_delivery_reservation",
            return_value=(True, "proactivity"),
        ),
        patch("relic.gumi_plugin.checkin_media_dispatcher.dispatch") as dispatch,
        patch("subprocess.Popen") as popen,
    ):
        assert hooks["transform_llm_output"](
            response_text="com'è andata la lezione?",
            platform="cron",
            session_id="cron_job_20260101_120000",
        ) == "[SILENT]"
    dispatch.assert_not_called()
    popen.assert_called_once()
