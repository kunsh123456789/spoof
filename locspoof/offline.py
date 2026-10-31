"""Offline packs: map tiles + houses + street data for a small area, saved on disk so the app keeps
working without internet.

A pack covers a bounding box. Downloading it fetches, once:
  * map tiles (zoom 12-17) from OpenStreetMap (kept small on purpose: the OSM tile servers ask users
    not to bulk-download, so areas and tile counts are capped),
  * the houses in the box (Overpass), used by trick-or-treat "Find houses",
  * the street network (Overpass), used by a small built-in A* router for walking/driving paths.

The online services are always tried first; this data is the fallback.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .errors import AppError
from .movement import EARTH_RADIUS_M, haversine_m, point_in_polygon, polygon_area_m2
from .trickortreat import USER_AGENT, HouseFinder, dedupe, parse_points

log = logging.getLogger("locspoof.offline")

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_ZOOM, MAX_ZOOM = 12, 17
MAX_TILES = 1500
MAX_AREA_M2 = 6_000_000      # ~2.4 km x 2.4 km
MIN_AREA_M2 = 20_000
GAP_FILL_M = 200.0           # unroutable legs up to this long become straight segments offline
SNAP_MAX_M = 150.0           # a waypoint further than this from any street is "not covered"
EST_TILE_KB = 18

BBox = tuple[float, float, float, float]  # south, west, north, east
Point = tuple[float, float]


# ---------------------------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------------------------

def validate_bbox(bbox: BBox) -> BBox:
    s, w, n, e = (float(v) for v in bbox)
    if not (-85 <= s < n <= 85 and -180 <= w < e <= 180):
        raise AppError("bad_area", "That map area is not valid.", "Zoom the map onto the neighborhood and try again.", 422)
    area = polygon_area_m2([(s, w), (s, e), (n, e), (n, w)])
    if area > MAX_AREA_M2:
        raise AppError(
            "area_too_big", f"That area is too big ({area / 1e6:.1f} km²).",
            f"Zoom in so the neighborhood fills the screen (at most {MAX_AREA_M2 / 1e6:.0f} km²).", 422,
        )
    if area < MIN_AREA_M2:
        raise AppError("area_too_small", "That area is tiny.", "Zoom out a little so a few streets are visible.", 422)
    return s, w, n, e


def lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_for_bbox(bbox: BBox, zooms=range(MIN_ZOOM, MAX_ZOOM + 1)) -> list[tuple[int, int, int]]:
    s, w, n, e = bbox
    out = []
    for z in zooms:
        x0, y1 = lonlat_to_tile(s, w, z)
        x1, y0 = lonlat_to_tile(n, e, z)
        out += [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return out


def bbox_contains(bbox: BBox, lat: float, lon: float) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


def roads_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    return f'[out:json][timeout:60];way["highway"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});out geom;'


def houses_query(bbox: BBox) -> str:
    from .trickortreat import HOUSE_TYPES
    box = "({:.6f},{:.6f},{:.6f},{:.6f})".format(*bbox)
    return (
        "[out:json][timeout:60];(\n"
        f'  way["building"~"^({HOUSE_TYPES})$"]{box};\n'
        f'  way["building"]["addr:housenumber"]{box};\n'
        f'  node["addr:housenumber"]{box};\n'
        ");out center 5000;"
    )


# ---------------------------------------------------------------------------------------------
# local street router
# ---------------------------------------------------------------------------------------------

_WALK_NO = {"motorway", "motorway_link", "construction", "proposed", "raceway", "bus_guideway"}
_DRIVE_OK = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service",
}


def _access(tags: dict, mode: str) -> tuple[bool, bool, bool]:
    """(allowed, forward, backward) for a way in the given mode."""
    hw = tags.get("highway", "")
    if mode == "walk":
        return hw not in _WALK_NO and tags.get("foot") != "no" and tags.get("access") != "no", True, True
    if hw not in _DRIVE_OK or tags.get("access") in ("no", "private") or tags.get("motor_vehicle") in ("no", "private"):
        return False, True, True
    oneway = tags.get("oneway")
    if oneway in ("-1", "reverse"):
        return True, False, True
    if oneway in ("yes", "1", "true") or tags.get("junction") == "roundabout" or hw in ("motorway", "motorway_link"):
        return True, True, False
    return True, True, True


class RoadGraph:
    def __init__(self) -> None:
        self.coords: dict[int, Point] = {}
        self.adj: dict[str, dict[int, list[tuple[int, float]]]] = {"walk": {}, "drive": {}}

    @classmethod
    def from_overpass(cls, data: dict) -> "RoadGraph":
        g = cls()
        for el in data.get("elements", []):
            if el.get("type") != "way" or "geometry" not in el or "nodes" not in el:
                continue
            ids, geom, tags = el["nodes"], el["geometry"], el.get("tags", {})
            if len(ids) != len(geom) or len(ids) < 2 or "highway" not in tags:
                continue
            for nid, p in zip(ids, geom):
                if p:  # clipped nodes outside the box come back as null
                    g.coords[nid] = (p["lat"], p["lon"])
            for mode in ("walk", "drive"):
                ok, fwd, back = _access(tags, mode)
                if not ok:
                    continue
                adj = g.adj[mode]
                for u, v in zip(ids, ids[1:]):
                    if u not in g.coords or v not in g.coords:
                        continue
                    d = haversine_m(*g.coords[u], *g.coords[v])
                    if fwd:
                        adj.setdefault(u, []).append((v, d))
                        adj.setdefault(v, [])
                    if back:
                        adj.setdefault(v, []).append((u, d))
                        adj.setdefault(u, [])
        return g

    def _nearest(self, p: Point, adj: dict) -> Optional[int]:
        k = math.cos(math.radians(p[0]))
        best, best_d = None, float("inf")
        for nid in adj:
            la, lo = self.coords[nid]
            d = (la - p[0]) ** 2 + ((lo - p[1]) * k) ** 2
            if d < best_d:
                best, best_d = nid, d
        if best is None or haversine_m(*p, *self.coords[best]) > SNAP_MAX_M:
            return None
        return best

    def route(self, a: Point, b: Point, mode: str) -> Optional[list[Point]]:
        adj = self.adj[mode]
        start, goal = self._nearest(a, adj), self._nearest(b, adj)
        if start is None or goal is None:
            return None
        goal_xy = self.coords[goal]
        open_: list[tuple[float, float, int]] = [(0.0, 0.0, start)]
        best = {start: 0.0}
        prev: dict[int, int] = {}
        while open_:
            _, g, u = heapq.heappop(open_)
            if u == goal:
                path = [u]
                while path[-1] in prev:
                    path.append(prev[path[-1]])
                pts = [self.coords[n] for n in reversed(path)]
                out: list[Point] = [a]
                for p in pts + [b]:
                    if out[-1] != p:
                        out.append(p)
                return out
            if g > best.get(u, float("inf")):
                continue
            for v, d in adj[u]:
                ng = g + d
                if ng < best.get(v, float("inf")):
                    best[v] = ng
                    prev[v] = u
                    heapq.heappush(open_, (ng + haversine_m(*self.coords[v], *goal_xy), ng, v))
        return None


# ---------------------------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------------------------

class OfflineStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.packs_dir = self.root / "packs"
        self.tiles_dir = self.root / "tiles"
        self._graphs: dict[str, RoadGraph] = {}
        self._houses: dict[str, list[Point]] = {}

    # -- metadata
    def list_packs(self) -> list[dict]:
        packs = []
        for meta in sorted(self.packs_dir.glob("*/pack.json")) if self.packs_dir.exists() else []:
            try:
                packs.append(json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("skipping unreadable pack %s", meta)
        packs.sort(key=lambda p: p.get("created", 0), reverse=True)
        return [{k: v for k, v in p.items() if k != "tiles"} for p in packs]

    def _meta(self, pack_id: str) -> Optional[dict]:
        try:
            return json.loads((self.packs_dir / pack_id / "pack.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def new_pack_dir(self, pack_id: str) -> Path:
        d = self.packs_dir / pack_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_meta(self, pack_id: str, meta: dict) -> None:
        (self.packs_dir / pack_id / "pack.json").write_text(json.dumps(meta), encoding="utf-8")

    def delete(self, pack_id: str) -> bool:
        if not re.fullmatch(r"[a-z0-9-]+", pack_id):
            return False
        meta = self._meta(pack_id)
        if meta is None:
            return False
        keep: set[tuple] = set()
        for other in self.packs_dir.glob("*/pack.json"):
            if other.parent.name != pack_id:
                try:
                    keep |= {tuple(t) for t in json.loads(other.read_text(encoding="utf-8")).get("tiles", [])}
                except (OSError, ValueError):
                    pass
        for z, x, y in (tuple(t) for t in meta.get("tiles", [])):
            if (z, x, y) not in keep:
                self.tile_path(z, x, y).unlink(missing_ok=True)
        shutil.rmtree(self.packs_dir / pack_id, ignore_errors=True)
        self._graphs.pop(pack_id, None)
        self._houses.pop(pack_id, None)
        return True

    # -- tiles
    def tile_path(self, z: int, x: int, y: int) -> Path:
        return self.tiles_dir / str(int(z)) / str(int(x)) / f"{int(y)}.png"

    # -- lookups used as fallbacks
    def _packs_with_bbox(self) -> list[tuple[str, BBox]]:
        return [(p["id"], tuple(p["bbox"])) for p in self.list_packs()]  # type: ignore[misc]

    def route(self, waypoints: list[Point], mode: str) -> Optional[list[list[Point]]]:
        """One polyline per leg from a pack covering every waypoint, or None."""
        for pid, bbox in self._packs_with_bbox():
            if not all(bbox_contains(bbox, la, lo) for la, lo in waypoints):
                continue
            g = self._graphs.get(pid) or self._load_graph(pid)
            if g is None:
                continue
            legs = []
            for a, b in zip(waypoints, waypoints[1:]):
                leg = g.route(a, b, mode)
                if leg is None and haversine_m(*a, *b) <= GAP_FILL_M:
                    leg = [a, b]  # a short hop across a gap in the street data (e.g. a cut-through)
                legs.append(leg)
            if all(legs):
                return legs  # type: ignore[return-value]
        return None

    def _load_graph(self, pid: str) -> Optional[RoadGraph]:
        try:
            g = RoadGraph.from_overpass(json.loads((self.packs_dir / pid / "roads.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        self._graphs[pid] = g
        return g

    def houses_near(self, lat: float, lon: float, radius_m: float, polygon: Optional[list[Point]] = None) -> list[Point]:
        out: list[Point] = []
        for pid, bbox in self._packs_with_bbox():
            if not (bbox_contains(bbox, lat, lon) or (polygon and any(bbox_contains(bbox, *p) for p in polygon))):
                continue
            pts = self._houses.get(pid)
            if pts is None:
                try:
                    pts = [tuple(p) for p in json.loads((self.packs_dir / pid / "houses.json").read_text(encoding="utf-8"))]
                except (OSError, ValueError):
                    continue
                self._houses[pid] = pts
            if polygon:
                out += [p for p in pts if point_in_polygon(p[0], p[1], polygon)]
            else:
                out += [p for p in pts if haversine_m(lat, lon, *p) <= radius_m]
        return dedupe(out)


# ---------------------------------------------------------------------------------------------
# downloader
# ---------------------------------------------------------------------------------------------

class OfflineDownloader:
    def __init__(self, store: OfflineStore, finder: HouseFinder, client: Optional[httpx.AsyncClient] = None, tile_delay: float = 0.3):
        self.store, self.finder, self._client, self.tile_delay = store, finder, client, tile_delay
        self.job: dict = {"state": "idle"}
        self._task: Optional[asyncio.Task] = None

    def estimate(self, bbox: BBox) -> dict:
        bbox = validate_bbox(bbox)
        tiles = tiles_for_bbox(bbox)
        if len(tiles) > MAX_TILES:
            raise AppError("too_many_tiles", f"That would need {len(tiles)} map tiles.", "Zoom in on a smaller area.", 422)
        s, w, n, e = bbox
        return {"tiles": len(tiles), "area_km2": round(polygon_area_m2([(s, w), (s, e), (n, e), (n, w)]) / 1e6, 2),
                "est_mb": round(len(tiles) * EST_TILE_KB / 1024 + 1.5, 1)}

    async def start(self, name: str, bbox: BBox) -> dict:
        name = " ".join(name.split())[:60]
        if not name:
            raise AppError("bad_name", "Give this offline area a name.", status=422)
        est = self.estimate(bbox)
        if self._task and not self._task.done():
            raise AppError("busy", "A download is already running.", "Wait for it to finish.", 409)
        bbox = validate_bbox(bbox)
        self.job = {"state": "running", "stage": "starting", "done": 0, "total": est["tiles"], "name": name}
        self._task = asyncio.create_task(self._run(name, bbox))
        return self.job

    async def _run(self, name: str, bbox: BBox) -> None:
        pack_id = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:24] or 'area'}-{uuid.uuid4().hex[:6]}"
        try:
            d = self.store.new_pack_dir(pack_id)
            self.job.update(stage="streets")
            roads = await self.finder.query_json(roads_query(bbox))
            (d / "roads.json").write_text(json.dumps(roads), encoding="utf-8")
            self.job.update(stage="houses")
            houses = dedupe(parse_points(await self.finder.query_json(houses_query(bbox))))
            (d / "houses.json").write_text(json.dumps(houses), encoding="utf-8")
            self.job.update(stage="map tiles", done=0)
            tiles, nbytes = tiles_for_bbox(bbox), 0
            failures = 0
            for i, (z, x, y) in enumerate(tiles, 1):
                path = self.store.tile_path(z, x, y)
                if not path.exists():
                    data = await self._fetch_tile(z, x, y)
                    if data is None:
                        failures += 1
                        if failures > max(5, len(tiles) // 10):
                            raise AppError("tiles_failed", "Could not download the map tiles.", "Check your internet connection and try again.", 502)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                if path.exists():
                    nbytes += path.stat().st_size
                self.job["done"] = i
                await asyncio.sleep(self.tile_delay)
            nbytes += sum(f.stat().st_size for f in d.glob("*.json"))
            self.store.write_meta(pack_id, {
                "id": pack_id, "name": name, "bbox": list(bbox), "created": time.time(), "bytes": nbytes,
                "houses": len(houses), "tile_count": len(tiles), "tiles": [list(t) for t in tiles],
            })
            self.job = {"state": "done", "pack_id": pack_id, "name": name, "done": len(tiles), "total": len(tiles)}
            log.info("offline pack %s ready (%d tiles, %d houses, %.1f MB)", pack_id, len(tiles), len(houses), nbytes / 1e6)
        except asyncio.CancelledError:
            shutil.rmtree(self.store.packs_dir / pack_id, ignore_errors=True)
            raise
        except Exception as e:
            log.exception("offline download failed")
            shutil.rmtree(self.store.packs_dir / pack_id, ignore_errors=True)
            err = e.to_dict() if isinstance(e, AppError) else {"code": "download_failed", "message": f"Download failed: {e}", "hint": None}
            self.job = {"state": "error", "error": err, "name": name}

    async def _fetch_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        for attempt in (1, 2):
            try:
                resp = await fetch_tile(self._client, z, x, y)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                log.warning("tile %d/%d/%d -> HTTP %s", z, x, y, resp.status_code)
            except httpx.HTTPError as e:
                log.warning("tile %d/%d/%d failed (attempt %d): %r", z, x, y, attempt, e)
            await asyncio.sleep(0.5)
        return None


async def fetch_tile(client: Optional[httpx.AsyncClient], z: int, x: int, y: int) -> httpx.Response:
    url, headers = TILE_URL.format(z=z, x=x, y=y), {"User-Agent": USER_AGENT}
    if client is not None:
        return await client.get(url, headers=headers, timeout=15)
    async with httpx.AsyncClient() as c:
        return await c.get(url, headers=headers, timeout=15)
