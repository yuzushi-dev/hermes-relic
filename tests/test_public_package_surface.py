import importlib
import inspect
import tomllib
from pathlib import Path


_OBSOLETE_SYMBOLS = {
    "ConsentManager",
    "ConsentType",
    "ConsentScope",
    "ConsentDecision",
    "is_capture_allowed",
    "EscalationNotifier",
    "EmergencyContact",
}


def test_public_package_surface_exposes_only_relic_gumi() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]

    assert project["description"] == "Native Hermes Gumi User plugin"
    assert project["scripts"] == {"relic-gumi": "relic.gumi_install:main"}
    assert {"relic", "relic-profile", "chronicle"}.isdisjoint(project["scripts"])


def test_canonical_imports_expose_no_obsolete_domain_symbols() -> None:
    packages = (
        importlib.import_module("relic"),
        importlib.import_module("relic.chronicle"),
        importlib.import_module("relic.control"),
        importlib.import_module("relic.gumi_install"),
    )

    for package in packages:
        assert _OBSOLETE_SYMBOLS.isdisjoint(dir(package))
        assert _OBSOLETE_SYMBOLS.isdisjoint(getattr(package, "__all__", ()))


def test_chronicle_emitters_use_runtime_scope_without_consent_basis() -> None:
    chronicle = importlib.import_module("relic.chronicle")

    for emitter_name in (
        "emit_event",
        "emit_decision",
        "emit_snapshot",
        "emit_provenance_edge",
        "start_span",
    ):
        parameters = inspect.signature(getattr(chronicle, emitter_name)).parameters
        assert "runtime_scope_id" in parameters
        assert "consent_basis" not in parameters


def test_obsolete_module_sources_are_absent() -> None:
    source_root = Path(__file__).parents[1] / "src" / "relic"

    for path in (
        source_root / "chronicle" / "consent_gate.py",
        source_root / "control" / "consent.py",
        source_root / "safety" / "escalation_notifier.py",
        source_root / "profile" / "_bootstrap_steps" / "consent.py",
        source_root / "profile" / "_bootstrap_steps" / "researcher_coded.py",
        source_root / "profile" / "_bootstrap_steps" / "self_report.py",
    ):
        assert not path.exists()
