"""Хранение credentials и seen message ids."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CredentialsStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not data.get("access_token"):
                log.warning("Credentials file exists but no access_token")
                return None
            return data
        except Exception as e:
            log.error("Failed to load credentials: %s", e)
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        log.info("Credentials saved to %s", self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
            log.info("Credentials cleared")


class SeenStore:
    """LRU-множество уже обработанных message_id."""

    def __init__(self, max_size: int = 5000):
        self._data: OrderedDict[int, None] = OrderedDict()
        self.max_size = max_size

    def __contains__(self, msg_id: int) -> bool:
        return msg_id in self._data

    def add(self, msg_id: int) -> None:
        if msg_id in self._data:
            self._data.move_to_end(msg_id)
            return
        self._data[msg_id] = None
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)
