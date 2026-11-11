"""Command-line installation for RELIC Gumi."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from pathlib import Path

from relic.hermes_plugin.install import (
    Installer,
    discover_hermes_home,
    ensure_profile_is_installer_owned,
)
from relic.hermes_plugin.lifecycle import LifecycleStore
from relic.onboarding.store import ProgressStore
from relic.paths import get_relic_home
from relic.profile.registry import ProfileRegistry
from relic.profile.user_onboarding import UserOnboarding

from relic.user_runtime import RuntimeActivation, RuntimeActivationError, activate_runtime


def _mark_profile_installer_owned(profile_home: Path) -> None:
    """Leave the lifecycle marker before onboarding saves canonical artifacts.

    Without it a ``configure`` run before ``install`` would create a profile the
    next ``install`` could not tell from someone else's. The state stays at
    ``preflight``, so outbound delivery remains blocked until install completes.
    """
    LifecycleStore(profile_home).claim()


def _onboard(hermes_profiles_home: Path):
    registry = ProfileRegistry(hermes_profiles_home=hermes_profiles_home)
    return UserOnboarding(registry).run()


def _activate_after_onboarding(
    onboarding: object, hermes_base_home: Path
) -> RuntimeActivation | None:
    return (
        None
        if onboarding is None
        else activate_runtime(hermes_base_home=hermes_base_home)
    )


_TELEGRAM_SETTINGS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS")


def _missing_telegram_settings(profile_home: Path) -> list[str]:
    """Names (never values) of required Telegram settings absent from the active Hermes secret scope."""
    try:
        values = ProfileRegistry(hermes_profiles_home=profile_home.parent)._read_private_env()
    except (OSError, ValueError):
        return list(_TELEGRAM_SETTINGS)
    return [name for name in _TELEGRAM_SETTINGS if not (values.get(name) or "").strip()]


def _telegram_progress_status(user_home: Path) -> str:
    try:
        return str(ProgressStore(user_home).load().get("status"))
    except (OSError, ValueError):
        return "unknown"


def _telegram_activation_error(user_home: Path) -> str | None:
    """Exception class name recorded when scheduling failed after a completed onboarding."""
    try:
        progress = ProgressStore(user_home).load()
    except (OSError, ValueError):
        return None
    error = progress.get("activation_error")
    if progress.get("status") != "complete" or not error:
        return None
    text = str(error)
    return text if re.fullmatch(r"[A-Za-z0-9_.]{1,80}", text) else "unknown"


def _clear_telegram_activation_error(profiles_home: Path) -> None:
    """Drop a stale ``activation_error`` once the CLI has activated successfully."""
    try:
        store = ProgressStore(ProfileRegistry(hermes_profiles_home=profiles_home).user_home)
        progress = store.load()
        if progress.pop("activation_error", None) is not None:
            store.save(progress)
    except (OSError, ValueError):
        pass


def _media_status(onboarding: object, name: str) -> str:
    media = getattr(onboarding, "media", {})
    setting = media.get(name) if isinstance(media, dict) else None
    return "on" if isinstance(setting, dict) and setting.get("selected") and setting.get("enabled") else "off"


def _recovery_command(profile_home: Path) -> str:
    return "relic-gumi configure --hermes-home " + shlex.quote(str(profile_home.parent.parent))


def _print_final_summary(
    profile_home: Path, onboarding: object, activation: RuntimeActivation
) -> None:
    delivery_ready = bool(
        getattr(onboarding, "delivery_capability_enabled", False)
        and getattr(onboarding, "delivery_execution_armed", False)
        and activation.recurring_active
    )
    print("relic-gumi summary:")
    print("dependencies: ready")
    print("plugin/hooks: ready")
    print("gateway: ready")
    print(f"Telegram: {'ready' if delivery_ready else 'needs setup'}")
    print(
        "media: "
        + "; ".join(
            f"{name} {_media_status(onboarding, name)}"
            for name in ("images", "audio", "music")
        )
    )
    print(f"cron: {'active' if activation.recurring_active else 'inactive'}")
    print(f"first message: {'scheduled' if activation.first_message_scheduled else 'waiting'}")
    if not delivery_ready:
        print(f"recovery: {_recovery_command(profile_home)}")
    print(f"relic-gumi ready: {profile_home}")


def _print_delivery_outcome(activation: RuntimeActivation) -> None:
    recurring = "scheduled" if activation.recurring_active else "not scheduled"
    first_message = "scheduled" if activation.first_message_scheduled else "not scheduled"
    print(f"relic-gumi delivery: recurring check-ins {recurring}; first message {first_message}.")

def _install(args: argparse.Namespace) -> int:
    installer = Installer(prompt=(lambda _question: True)) if args.yes else Installer()
    try:
        result = installer.install(
            hermes_home=args.hermes_home,
            non_interactive=args.non_interactive,
            start_gateway=not args.no_start,
        )
    except Exception as exc:
        print(f"relic-gumi installation blocked: {exc}", file=sys.stderr)
        return 1
    model_import = getattr(result, "model_import", None)
    for message in getattr(model_import, "messages", ()):
        print(f"relic-gumi model: {message}")
    if args.no_start:
        print(f"relic-gumi reconciled offline: {result.profile_home}")
        return 0
    print(f"relic-gumi ready: {result.profile_home}")
    print("Next: open Telegram and send /start to your bot")
    missing = _missing_telegram_settings(result.profile_home)
    if missing:
        print("Missing in the active Hermes secret scope: " + ", ".join(missing))
    return 0


def _configure(args: argparse.Namespace) -> int:
    if args.non_interactive:
        print("relic-gumi configure requires an interactive terminal.", file=sys.stderr)
        return 1
    try:
        location = discover_hermes_home(hermes_home=args.hermes_home)
        ensure_profile_is_installer_owned(location.profile_home)
        _mark_profile_installer_owned(location.profile_home)
        onboarding = _onboard(location.profile_home.parent)
        activation = _activate_after_onboarding(onboarding, location.base_home)
    except RuntimeActivationError as exc:
        print(f"relic-gumi activation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"relic-gumi configure failed: {exc}", file=sys.stderr)
        return 1
    if activation is not None:
        _clear_telegram_activation_error(location.profile_home.parent)
    if activation is not None and isinstance(getattr(onboarding, "media", None), dict):
        _print_final_summary(location.profile_home, onboarding, activation)
    elif activation is not None:
        _print_delivery_outcome(activation)
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input(
            "This will remove the RELIC Gumi plugin, its hooks, and its scheduled "
            "jobs from Hermes. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("relic-gumi uninstall: aborted.", file=sys.stderr)
            return 1

    user_dir = get_relic_home() / "user"
    if args.erase_data and not args.yes:
        answer = input(
            f"This will PERMANENTLY DELETE {user_dir} "
            "(identity, baseline responses, preferences). Type 'erase' to confirm: "
        )
        if answer.strip() != "erase":
            print("relic-gumi uninstall: aborted, data not erased.", file=sys.stderr)
            return 1

    installer = Installer()
    try:
        result = installer.uninstall(hermes_home=args.hermes_home)
    except Exception as exc:
        print(f"relic-gumi uninstall failed: {exc}", file=sys.stderr)
        return 1

    print(f"relic-gumi uninstall: plugin {'removed' if result.plugin_removed else 'already absent'}")
    print(f"relic-gumi uninstall: {len(result.removed_jobs)} scheduled job(s) removed")

    if args.erase_data:
        if user_dir.exists():
            shutil.rmtree(user_dir)
            print(f"relic-gumi uninstall: erased {user_dir}")
        else:
            print(f"relic-gumi uninstall: {user_dir} already absent")
    else:
        print(f"relic-gumi uninstall: user data preserved at {user_dir}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    relic_home = args.relic_home or get_relic_home()
    registry = ProfileRegistry(relic_home=relic_home)
    findings: list[str] = []
    ok = True

    def _mode(path: Path) -> int:
        return path.stat().st_mode & 0o777

    if not registry.user_home.exists():
        print(f"relic-gumi verify: {registry.user_home} does not exist.", file=sys.stderr)
        return 1

    if registry.user_home.is_symlink():
        findings.append(f"[FAIL] {registry.user_home} is a symlink")
        ok = False
    elif _mode(registry.user_home) != 0o700:
        findings.append(f"[FAIL] {registry.user_home} has mode {oct(_mode(registry.user_home))}, expected 0o700")
        ok = False
    else:
        findings.append(f"[OK] {registry.user_home}")

    onboarding_paths = (
        registry.baseline_response_path,
        registry.gumi_profile_path,
        registry.runtime_policy_path,
        registry.checkin_state_path,
    )
    # "Never onboarded" (User initialized, none of the onboarding artifacts yet,
    # Telegram onboarding not complete) is pending, not broken.
    onboarding_pending = (
        registry.user_profile_path.exists()
        and not any(path.exists() or path.is_symlink() for path in onboarding_paths)
        and _telegram_progress_status(registry.user_home) != "complete"
    )

    for path in (registry.user_profile_path, *onboarding_paths):
        if not path.exists():
            if onboarding_pending:
                continue
            findings.append(f"[MISSING] {path}")
            ok = False
            continue
        if path.is_symlink():
            findings.append(f"[FAIL] {path} is a symlink")
            ok = False
            continue
        if _mode(path) != 0o600:
            findings.append(f"[FAIL] {path} has mode {oct(_mode(path))}, expected 0o600")
            ok = False
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(f"[FAIL] {path} is not valid JSON: {exc}")
            ok = False
            continue
        findings.append(f"[OK] {path}")

    if onboarding_pending:
        findings.append("[PENDING] onboarding not completed on Telegram")

    activation_error = _telegram_activation_error(registry.user_home)
    if activation_error:
        findings.append(
            f"[WARN] onboarding saved but scheduling failed ({activation_error}); "
            "send /gumi-setup retry"
        )

    if not args.skip_cron:
        try:
            installer = Installer()
            for entry in installer.check_managed_cron(hermes_home=args.hermes_home):
                status, name = entry.split(":", 1)
                findings.append(f"[{'OK' if status == 'ok' else status.upper()}] cron:{name}")
                if status != "ok":
                    ok = False
        except Exception as exc:
            findings.append(f"[FAIL] cron check: {exc}")
            ok = False

    for line in findings:
        print(line)
    print(f"relic-gumi verify: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relic-gumi", description="Install RELIC Gumi into Hermes.")
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install", help="Install RELIC Gumi into Hermes.")
    install.add_argument("--hermes-home", type=Path, help="Override the Hermes base home.")
    install.add_argument("--non-interactive", action="store_true", help="Never prompt; print remediation instead.")
    install.add_argument("--yes", action="store_true", help="Approve the one prerequisite install prompt.")
    install.add_argument(
        "--no-start",
        action="store_true",
        help="Reconcile plugin and cron only while an already stopped gateway remains stopped.",
    )
    install.set_defaults(handler=_install)

    configure = commands.add_parser(
        "configure",
        help="Rerun RELIC Gumi User configuration (advanced; normal onboarding happens on Telegram).",
    )
    configure.add_argument("--hermes-home", type=Path, help="Override the Hermes base home.")
    configure.add_argument(
        "--non-interactive", action="store_true", help="Never prompt; report the remediation instead."
    )
    configure.set_defaults(handler=_configure)

    uninstall = commands.add_parser(
        "uninstall", help="Remove the RELIC Gumi plugin, hooks, and scheduled jobs from Hermes."
    )
    uninstall.add_argument("--hermes-home", type=Path, help="Override the Hermes base home.")
    uninstall.add_argument(
        "--erase-data", action="store_true",
        help="Also permanently delete $RELIC_HOME/user/ (identity, baseline, preferences).",
    )
    uninstall.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    uninstall.set_defaults(handler=_uninstall)

    verify = commands.add_parser(
        "verify", help="Check the canonical User data and cron ownership for integrity, read-only."
    )
    verify.add_argument("--hermes-home", type=Path, help="Override the Hermes base home.")
    verify.add_argument("--relic-home", type=Path, help="Override the RELIC home.")
    verify.add_argument(
        "--skip-cron", action="store_true", help="Skip the read-only Hermes cron reconciliation check."
    )
    verify.set_defaults(handler=_verify)

    args = parser.parse_args(argv)
    return args.handler(args)
