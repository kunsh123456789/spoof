"""California limits: the map can be restricted to the state, so the detail (stop signs, traffic lights) is the best it can be."""
# (south, west, north, east): a rectangle around the state
CA_BOX = (32.40, -124.60, 42.10, -114.00)
CA_CENTER = (37.2, -119.5)


def in_california(lat: float, lon: float) -> bool:
    s, w, n, e = CA_BOX
    return s <= lat <= n and w <= lon <= e
