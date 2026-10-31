"""Crash-safe file writing shared by every data file (favorites, settings, routes, offline tiles, ...)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path | str, data: bytes) -> None:
    """Write a file so that it is either complete or absent, never half written.

    The bytes go to a temporary file in the same folder, are flushed to the disk (fsync), and only then replace the
    real file in one step. A crash, a full disk or a power cut at any point leaves the old file (or nothing) in place.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
