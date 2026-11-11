from relic.profile.baseline_artifact import build_baseline_artifact


def test_baseline_artifact_neither_requires_nor_emits_risk_flags() -> None:
    artifact = build_baseline_artifact(
        {
            "bootstrap_session_id": "bootstrap",
            "researcher_id": "",
            "subject_id": "user",
            "baseline_method": "item_battery",
            "self_report_fields": {},
            "researcher_coded_fields": {},
            "interaction_preferences": {},
            "relational_expectations": {},
            "boundaries": {},
            "opt_out_categories": {"values": [], "origin": "subject-stated"},
            "item_battery": {"responses": []},
        }
    )

    assert "risk_flags" not in artifact
