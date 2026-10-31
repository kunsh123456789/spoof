import math

import pytest

from locspoof.movement import haversine_m, step_towards, validate_coords


def test_haversine_known_distance():
    # Paris -> London is ~343.5 km
    assert haversine_m(48.8566, 2.3522, 51.5074, -0.1278) == pytest.approx(343_500, rel=0.01)


def test_step_moves_expected_distance_and_arrives():
    lat, lon, arrived = step_towards(0, 0, 0, 1, 1000)
    assert not arrived
    assert haversine_m(0, 0, lat, lon) == pytest.approx(1000, rel=1e-3)
    assert step_towards(0, 0, 0, 0.0001, 1000) == (0, 0.0001, True)


def test_walk_converges_monotonically():
    pos, last = (10.0, 10.0), math.inf
    for _ in range(1000):
        *pos, arrived = step_towards(*pos, 10.05, 10.05, 100)
        d = haversine_m(*pos, 10.05, 10.05)
        assert d <= last
        last = d
        if arrived:
            break
    else:
        pytest.fail("never arrived")


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181), (float("nan"), 0), (0, float("inf"))])
def test_validate_rejects(lat, lon):
    with pytest.raises(ValueError):
        validate_coords(lat, lon)


def test_validate_accepts_edges():
    validate_coords(90, 180)
    validate_coords(-90, -180)
