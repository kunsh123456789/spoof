import math
import random

import pytest

from locspoof.movement import PathFollower, bearing_deg, haversine_m, turn_angle_deg
from locspoof.realism import NoiseModel, WalkModel, cooldown_minutes, traffic_stop_seconds


def test_noise_is_bounded_smooth_and_centered():
    m = NoiseModel(rng=random.Random(1))
    pts = [m.apply(40.0, -75.0, 1.0) for _ in range(2000)]
    dists = [haversine_m(40.0, -75.0, *p) for p in pts]
    assert max(dists) <= 6 * math.sqrt(2) + 0.1              # hard cap
    assert 0.5 < sum(dists) / len(dists) < 4                  # a metre or two on average, not zero, not huge
    steps = [haversine_m(*a, *b) for a, b in zip(pts, pts[1:])]
    assert sum(steps) / len(steps) < sum(dists) / len(dists)  # drifts smoothly: consecutive points are closer than random ones
    assert abs(sum(p[0] - 40.0 for p in pts) / len(pts)) < 2e-5 and len(set(pts)) > 1500


def test_noise_clamps_at_poles():
    lat, lon = NoiseModel(sigma_m=500, max_m=500, rng=random.Random(2)).apply(89.99999, 179.99999, 1)
    assert -90 <= lat <= 90 and -180 <= lon <= 180


def test_walk_model_stays_in_band_and_eases_into_corners():
    w = WalkModel(random.Random(3))
    fs = [w.factor(1.0) for _ in range(500)]
    assert min(fs) >= 0.85 - 1e-9 and max(fs) <= 1.15 + 1e-9 and len(set(round(f, 3) for f in fs)) > 50
    w2 = WalkModel(random.Random(3))
    straight, corner = w2.factor(1.0, 50, 90), WalkModel(random.Random(3)).factor(1.0, 2, 90)
    assert corner < straight * 0.8
    assert WalkModel(random.Random(3)).factor(1.0, 2, 10) == pytest.approx(WalkModel(random.Random(3)).factor(1.0))  # gentle bends don't slow


@pytest.mark.parametrize("km,lo,hi", [(0.4, 0, 0), (1, 1, 1), (10, 6, 6), (50, 17, 19), (1000, 78, 78), (99999, 120, 120)])
def test_cooldown_table(km, lo, hi):
    assert lo - 0.01 <= cooldown_minutes(km) <= hi + 0.01


def test_cooldown_is_monotonic():
    vals = [cooldown_minutes(k) for k in range(0, 3000, 7)]
    assert vals == sorted(vals)


def test_traffic_stops_are_more_likely_at_sharp_turns():
    def rate(angle):
        rng = random.Random(4)
        return sum(traffic_stop_seconds(angle, rng) > 0 for _ in range(4000)) / 4000

    assert rate(5) < rate(50) < rate(120)
    assert all(4 <= traffic_stop_seconds(120, random.Random(i)) <= 28 for i in range(50) if traffic_stop_seconds(120, random.Random(i)))


def test_bearing_and_turn_angle():
    assert bearing_deg((0, 0), (1, 0)) == pytest.approx(0, abs=1e-6)
    assert bearing_deg((0, 0), (0, 1)) == pytest.approx(90, abs=1e-6)
    assert turn_angle_deg((0, 0), (0, 1), (1, 1)) == pytest.approx(90, abs=0.1)
    assert turn_angle_deg((0, 0), (0, 1), (0, 2)) == pytest.approx(0, abs=0.01)
    assert turn_angle_deg((0, 0), (0, 1), (0, 0)) == pytest.approx(180, abs=0.01)


def test_path_follower_reports_corners_and_passed_turns():
    f = PathFollower([(0, 0), (0, 0.001), (0.001, 0.001)])
    dist, angle = f.upcoming_turn()
    assert dist == pytest.approx(haversine_m(0, 0, 0, 0.001), rel=1e-6) and angle == pytest.approx(90, abs=0.5)
    f.advance(dist - 5)
    assert f.upcoming_turn()[0] == pytest.approx(5, abs=0.2) and f.passed_turns == []
    f.advance(10)                                     # crosses the corner
    assert len(f.passed_turns) == 1 and f.passed_turns[0] == pytest.approx(90, abs=0.5)
    f.advance(1)
    assert f.passed_turns == []                       # reset on every advance
    assert PathFollower([(0, 0), (0, 1)]).upcoming_turn()[1] == 0     # last vertex has no turn
