"""GPX import: turn a GPX file's track (or route / waypoints) into a list of points."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from defusedxml import ElementTree as ET

from .errors import AppError
from .movement import haversine_m, validate_coords

MAX_POINTS = 20_000
MAX_BYTES = 10 * 1024 * 1024
_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _parse_time(text: Optional[str]) -> Optional[float]:
    m = _TIME.match((text or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        return None
    off = m.group(7)
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        return dt.timestamp() - sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return dt.timestamp()


def parse_gpx(data: bytes) -> dict:
    """Return {points, name, distance_m, duration_s, avg_kmh}. Raises AppError for unusable files."""
    if len(data) > MAX_BYTES:
        raise AppError("bad_gpx", "That file is too large.", "GPX files over 10 MB are not supported.", 413)
    try:
        root = ET.fromstring(data)
    except Exception as e:  # defusedxml raises several types, including for entity-expansion attacks
        raise AppError("bad_gpx", "That does not look like a valid GPX file.", str(e)[:120], 422) from e
    if _local(root.tag) != "gpx":
        raise AppError("bad_gpx", "That does not look like a GPX file.", "The root element must be <gpx>.", 422)

    def collect(kinds: set[str]) -> list[tuple[float, float, Optional[float]]]:
        out = []
        for el in root.iter():
            if _local(el.tag) in kinds:
                try:
                    lat, lon = float(el.attrib["lat"]), float(el.attrib["lon"])
                    validate_coords(lat, lon)
                except (KeyError, ValueError) as e:
                    raise AppError("bad_gpx", "The file has a point without valid coordinates.", status=422) from e
                t = next((_parse_time(c.text) for c in el if _local(c.tag) == "time"), None)
                out.append((lat, lon, t))
        return out

    pts = collect({"trkpt"}) or collect({"rtept"}) or collect({"wpt"})
    if len(pts) < 2:
        raise AppError("bad_gpx", "The file needs at least 2 points.", "Use a GPX track or route.", 422)
    # drop immediate duplicates (some loggers repeat points while stationary)
    dedup = [pts[0]]
    for p in pts[1:]:
        if (p[0], p[1]) != (dedup[-1][0], dedup[-1][1]):
            dedup.append(p)
    if len(dedup) > MAX_POINTS:
        raise AppError("bad_gpx", f"The track has {len(dedup):,} points (max {MAX_POINTS:,}).", "Simplify it in a GPX editor first.", 422)
    if len(dedup) < 2:
        raise AppError("bad_gpx", "All points in the file are the same place.", status=422)
    dist = sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(dedup, dedup[1:]))
    times = [p[2] for p in dedup if p[2] is not None]
    duration = (max(times) - min(times)) if len(times) >= 2 else None
    avg = round(dist / duration * 3.6, 1) if duration and duration > 0 else None
    name = next((e.text.strip() for e in root.iter() if _local(e.tag) == "name" and e.text and e.text.strip()), None)
    return {
        "points": [[round(p[0], 6), round(p[1], 6)] for p in dedup], "name": name,
        "distance_m": round(dist, 1), "duration_s": duration, "avg_kmh": avg,
    }
