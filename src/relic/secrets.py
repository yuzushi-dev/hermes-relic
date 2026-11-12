"""Profile-scoped credentials supplied exclusively by Hermes.

Delegate scope semantics to Hermes; never read credential files or add a direct
process-environment fallback. Resolve on demand, not at import time.
Hermes may use the process environment in single-profile mode without a scope;
multiplexing without a scope raises UnscopedSecretError.
"""
from __future__ import annotations

SECRET_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
    "GEMINI_API_KEY",
    "OLLAMA_API_KEYS",
)


def secret_value(name: str) -> str:
    """Return the Hermes-resolved value, or absent when its API fails.

    Older hosts without this API and UnscopedSecretError both fail closed.
    Never log exception details: a credential provider may include secrets.
    """
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
    except Exception:
        return ""
    return value.strip() if isinstance(value, str) else ""


def scoped_credentials() -> dict[str, str]:
    """Take a short-lived snapshot in the caller's active Hermes scope."""
    return {name: value for name in SECRET_NAMES if (value := secret_value(name))}


def credential_setup_commands() -> str:
    """Instructions only: agents must not receive or execute secret values."""
    return (
        "Recommended: Hermes Desktop -> Settings -> Plugins, select relic-gumi "
        "in the target profile and use the masked secret fields.\n"
        "CLI alternative: the user must run these commands themselves, replacing "
        "<value> privately. Values appear in the command line and may remain in "
        "shell history. Agents must never read, print, or pass these values.\n"
        + "\n".join(
            f"hermes --profile relic-gumi config set {name} <value>"
            for name in SECRET_NAMES
        )
    )
