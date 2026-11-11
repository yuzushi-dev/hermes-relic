from __future__ import annotations

import os
import stat

import pytest

from relic.onboarding.store import ProgressStore


def _data(tz: str = "Europe/Rome") -> dict:
    return {
        "answers": {"tz": tz},
        "chat_id": "123456789",
        "user_id": "123456789",
        "status": "in_progress",
    }


def test_roundtrip_and_permissions(tmp_path):
    s = ProgressStore(tmp_path / "user")
    s.save(_data())
    assert s.load()["answers"]["tz"] == "Europe/Rome"
    assert stat.S_IMODE(os.stat(s.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "user").st_mode) == 0o700
    assert not list((tmp_path / "user").glob("*.tmp"))


def test_two_consecutive_saves_leave_no_tmp_and_last_wins(tmp_path):
    s = ProgressStore(tmp_path / "user")
    s.save(_data("Europe/Rome"))
    s.save(_data("UTC"))
    assert s.load()["answers"]["tz"] == "UTC"
    assert list((tmp_path / "user").iterdir()) == [s.path]


def test_load_missing_returns_default(tmp_path):
    loaded = ProgressStore(tmp_path / "user").load()
    assert loaded["status"] == "new"
    assert loaded["answers"] == {}


def test_save_rejects_symlink(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (user / "onboarding_progress.json").symlink_to(target)
    s = ProgressStore(user)
    with pytest.raises(OSError):
        s.save(_data())
    with pytest.raises(OSError):
        s.load()
    assert target.read_text() == "{}"
