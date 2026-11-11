from __future__ import annotations

import pytest
import sys
import types


@pytest.fixture
def scoped_secrets(monkeypatch):
    """A synthetic active Hermes scope, isolated from ambient credentials."""
    from agent.secret_scope import (set_secret_scope, reset_secret_scope, set_multiplex_context, reset_multiplex_context)
    values = {}
    scope = set_secret_scope(values)
    multiplex = set_multiplex_context(True)
    try:
        yield values
    finally:
        reset_secret_scope(scope)
        reset_multiplex_context(multiplex)


@pytest.fixture
def inline_cron_dispatch(monkeypatch):
    """Run the detached cron delivery in-process, as its child process would."""
    from relic.hermes_plugin import directory_plugin

    def spawn(message_text, kind, hermes_home, relic_home, runtime_scope_id):
        directory_plugin._dispatch_and_record(
            message_text, kind, hermes_home, relic_home, runtime_scope_id
        )

    monkeypatch.setattr(directory_plugin, "_spawn_dispatch", spawn)
