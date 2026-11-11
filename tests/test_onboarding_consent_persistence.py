from __future__ import annotations

import json
from pathlib import Path

from relic.profile.registry import ProfileRegistry
import relic.profile.user_runtime as ur


def _runtime(tmp_path: Path):
    registry = ProfileRegistry(
        relic_home=tmp_path,
        hermes_profiles_home=tmp_path / "hermes-profiles",
    )
    user = registry.initialize_user()
    registry.materialize_user_artifacts(
        user,
        baseline={"timezone": "UTC"},
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {
                "proactivity": {
                    "recurring_messages": "on",
                    "quiet_hours": "22:00-08:00",
                    "maximum_frequency": "1/day",
                }
            },
            "media": {
                name: {"selected": False, "enabled": False}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": True,
            "delivery_execution_armed": True,
            "first_message": "wait",
            "credential_references": {},
        },
        checkin_state={
            "recurring": {"status": "armed"},
            "first_message": {"status": "waiting"},
        },
        private_env={},
    )
    return ur._load_runtime(tmp_path, user.runtime_scope_id)


def test_consents_survive_second_store_path_call(tmp_path):
    runtime = _runtime(tmp_path)
    _db, _baseline, policy_path, _queue = ur._checkin_store_paths(runtime)
    assert json.loads(policy_path.read_text())["consent_for_active_elicitation"] is False
    policy_path.write_text(
        json.dumps(
            {
                "consent_for_active_elicitation": True,
                "consent_for_passive_extraction": True,
                "PRO_IMAGE": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ur._checkin_store_paths(runtime)
    data = json.loads(policy_path.read_text())
    assert data["consent_for_active_elicitation"] is True
    assert data["consent_for_passive_extraction"] is True
    assert data["PRO_IMAGE"] == 1
