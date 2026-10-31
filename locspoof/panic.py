"""The one "panic location": where the phone jumps to when you press the panic button."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .errors import AppError
from .fileio import atomic_write
from .movement import validate_coords

log = logging.getLogger("locspoof.panic")


class PanicStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def get(self) -> Optional[dict]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            validate_coords(d["lat"], d["lon"])
            return {"lat": float(d["lat"]), "lon": float(d["lon"]), "name": str(d.get("name") or "")[:80]}
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("ignoring unreadable %s", self.path)
            return None

    def set(self, lat: float, lon: float, name: str = "") -> dict:
        try:
            validate_coords(lat, lon)
        except ValueError as e:
            raise AppError("bad_coords", str(e), status=422) from e
        item = {"lat": float(lat), "lon": float(lon), "name": (name or "").strip()[:80]}
        atomic_write(self.path, json.dumps(item, indent=2).encode("utf-8"))
        return item

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
