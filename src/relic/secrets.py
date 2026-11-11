"""Profile-scoped credentials supplied exclusively by Hermes.

Never fall back to process environment or credential files: under multiplexing
those may belong to another profile. Resolve on demand, not at import time.
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
    """Return a scoped value, or absent when Hermes cannot provide a scope.

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
    """Value-free instructions; credential entry belongs to Hermes, not Relic."""
    return "\n".join(
        f"hermes --profile relic-gumi config set {name}"
        for name in SECRET_NAMES if name != "OLLAMA_API_KEYS"
    )
