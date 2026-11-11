"""Explicitly seed synthetic Hermes scopes in behavior-test setup helpers."""
from agent.secret_scope import current_secret_scope


def seed_credentials(values):
    scope = current_secret_scope()
    assert isinstance(scope, dict), "test requires the scoped_secrets fixture"
    scope.update(values)


def onboard_with_scoped_credentials(*args, environ=None, **kwargs):
    from relic.profile.user_onboarding import UserOnboarding
    seed_credentials(environ or {})
    return UserOnboarding(*args, environ={}, **kwargs)
