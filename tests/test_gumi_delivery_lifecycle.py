from __future__ import annotations
from relic.hermes_runtime import HERMES_USER_PROFILE_NAME

from pathlib import Path

from relic.gumi_plugin.checkin_media_dispatcher import dispatch


def test_dispatch_blocks_outbound_delivery_until_installer_is_ready(
    tmp_path: Path, capsys
) -> None:
    result = dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "profiles" / HERMES_USER_PROFILE_NAME,
        relic_user_home=tmp_path / "user",
    )

    assert result == {
        "tipo": "text",
        "success": False,
        "reason": "lifecycle_not_ready",
        "retry_after": 90,
    }
    assert "gateway-ready" in capsys.readouterr().err
