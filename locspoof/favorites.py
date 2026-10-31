"""Named locations stored in a local JSON file (atomic writes)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path

from .errors import AppError
from .movement import validate_coords

log = logging.getLogger("locspoof.favorites")


class FavoritesStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        with self._lock:
            return self._read()

    def add(self, name: str, lat: float, lon: float) -> dict:
        name = name.strip()
        if not name:
            raise AppError("bad_name", "Give the favorite a name.", status=422)
        try:
            validate_coords(lat, lon)
        except ValueError as e:
            raise AppError("bad_coords", str(e), status=422) from e
        item = {"id": uuid.uuid4().hex[:12], "name": name[:80], "lat": lat, "lon": lon}
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        return item

    def delete(self, fav_id: str) -> bool:
        with self._lock:
            items = self._read()
            kept = [i for i in items if i["id"] != fav_id]
            if len(kept) == len(items):
                return False
            self._write(kept)
            return True

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [i for i in data if {"id", "name", "lat", "lon"} <= i.keys()]
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, TypeError, AttributeError):
            backup = self.path.with_suffix(".corrupt.json")
            log.error("%s is not valid; moving it to %s", self.path, backup)
            os.replace(self.path, backup)
            return []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
