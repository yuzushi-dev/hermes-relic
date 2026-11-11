"""Resumable onboarding progress, stored privately under the user home.

Holds answers, chat/user ids, status and the last question message id.
Never store tokens or other secrets here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ProgressStore:
    def __init__(self, user_home: Path):
        self.user_home = Path(user_home)
        self.path = self.user_home / "onboarding_progress.json"

    def _check_path(self) -> None:
        if self.path.is_symlink():
            raise OSError("onboarding progress path is a symlink")
        if self.path.exists() and not self.path.is_file():
            raise OSError("onboarding progress path is not a regular file")

    def load(self) -> dict[str, Any]:
        self._check_path()
        if not self.path.exists():
            return {"status": "new", "answers": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("onboarding progress is invalid")
        data.setdefault("status", "new")
        if not isinstance(data.get("answers"), dict):
            data["answers"] = {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        if self.user_home.is_symlink():
            raise OSError("onboarding user home is a symlink")
        self.user_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_path()
        tmp = self.path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)
