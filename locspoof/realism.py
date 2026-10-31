"""Small models that make simulated movement look less robotic (all pure and seedable for tests)."""
from __future__ import annotations

import math
import random
from typing import Optional

from .movement import EARTH_RADIUS_M

# (jump km, suggested wait minutes): a rough "how long would a real trip take" table
_COOLDOWN = [(1, 1), (5, 2), (10, 6), (25, 11), (30, 14), (65, 22), (81, 25), (100, 35),
             (250, 45), (500, 60), (750, 75), (1000, 78), (1500, 120)]


def cooldown_minutes(km: float) -> float:
    """Suggested wait after a location jump of ``km`` (0 for jumps under 1 km). Piecewise linear, capped at 2 h."""
    if km < 1:
        return 0.0
    for (k0, m0), (k1, m1) in zip(_COOLDOWN, _COOLDOWN[1:]):
        if km <= k1:
            return m0 + (m1 - m0) * (km - k0) / (k1 - k0)
    return float(_COOLDOWN[-1][1])


class NoiseModel:
    """Smooth, bounded GPS-like drift (an AR(1) random walk in metres), not white noise."""

    def __init__(self, sigma_m: float = 1.8, tau_s: float = 6.0, max_m: float = 6.0, rng: Optional[random.Random] = None):
        self.sigma, self.tau, self.max = sigma_m, tau_s, max_m
        self.rng = rng or random.Random()
        self.n = self.e = 0.0

    def offset(self, dt: float) -> tuple[float, float]:
        a = math.exp(-max(dt, 1e-3) / self.tau)
        s = self.sigma * math.sqrt(1 - a * a)
        self.n = max(-self.max, min(self.max, a * self.n + self.rng.gauss(0, s)))
        self.e = max(-self.max, min(self.max, a * self.e + self.rng.gauss(0, s)))
        return self.n, self.e

    def apply(self, lat: float, lon: float, dt: float) -> tuple[float, float]:
        n, e = self.offset(dt)
        dlat = math.degrees(n / EARTH_RADIUS_M)
        dlon = math.degrees(e / (EARTH_RADIUS_M * max(math.cos(math.radians(lat)), 0.01)))
        return max(-90.0, min(90.0, lat + dlat)), max(-180.0, min(180.0, lon + dlon))


class WalkModel:
    """Natural pace: a slowly wandering speed factor plus easing off before sharp corners."""

    def __init__(self, rng: Optional[random.Random] = None, spread: float = 0.15):
        self.rng = rng or random.Random()
        self.spread = spread
        self.f = 1.0

    def factor(self, dt: float, dist_to_turn_m: Optional[float] = None, turn_deg: float = 0.0) -> float:
        # mean-reverting random walk, kept inside 1 +/- spread
        self.f += (1.0 - self.f) * min(1.0, 0.15 * dt) + self.rng.gauss(0, 0.04 * math.sqrt(max(dt, 1e-3)))
        self.f = max(1 - self.spread, min(1 + self.spread, self.f))
        slow = 1.0
        if dist_to_turn_m is not None and dist_to_turn_m < 10 and turn_deg > 40:
            slow = 0.55 + 0.45 * (dist_to_turn_m / 10)
        return self.f * slow


def traffic_stop_seconds(turn_deg: float, rng: random.Random) -> float:
    """Seconds to wait at a junction on a driving route (0 = keep going). Stops are likelier at sharp turns."""
    p = 0.15 if turn_deg < 35 else 0.35 if turn_deg < 75 else 0.5
    return round(rng.uniform(4, 28), 1) if rng.random() < p else 0.0
