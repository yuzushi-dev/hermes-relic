from __future__ import annotations

import pytest


@pytest.fixture
def inline_cron_dispatch(monkeypatch):
    """Run the detached cron delivery in-process, as its child process would."""
    from relic.hermes_plugin import directory_plugin

    def spawn(message_text, kind, hermes_home, relic_home, runtime_scope_id):
        directory_plugin._dispatch_and_record(
            message_text, kind, hermes_home, relic_home, runtime_scope_id
        )

    monkeypatch.setattr(directory_plugin, "_spawn_dispatch", spawn)
