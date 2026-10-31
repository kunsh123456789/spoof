"""Remembers each phone's last-seen iOS version (devices.json) so an iOS update can be noticed."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("locspoof.memory")


class DeviceMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (FileNotFoundError, ValueError, OSError):
            self.data = {}

    def remember(self, udid: str, name: str, version: str) -> Optional[str]:
        """Record the phone and return the iOS version we saw last time (None the first time)."""
        key = udid.replace("-", "").lower()
        prev = (self.data.get(key) or {}).get("ios_version")
        if prev != version or self.data.get(key, {}).get("name") != name:
            self.data[key] = {"name": name, "ios_version": version}
            self._save()
        return prev

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not save %s", self.path)
