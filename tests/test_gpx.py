import pytest

from locspoof.errors import AppError
from locspoof.gpx import MAX_POINTS, parse_gpx

NS = 'xmlns="http://www.topografix.com/GPX/1/1"'


def gpx(body, ns=NS):
    return f'<?xml version="1.0"?><gpx {ns} version="1.1" creator="t">{body}</gpx>'.encode()


def trkpt(lat, lon, t=None):
    return f'<trkpt lat="{lat}" lon="{lon}">' + (f"<time>{t}</time>" if t else "") + "</trkpt>"


def test_track_with_times_computes_distance_and_pace():
    body = "<trk><name>Morning run</name><trkseg>" + trkpt(40.0, -75.0, "2024-05-01T10:00:00Z") + \
        trkpt(40.0, -74.999, "2024-05-01T10:00:30Z") + trkpt(40.0, -74.998, "2024-05-01T10:01:00Z") + "</trkseg></trk>"
    r = parse_gpx(gpx(body))
    assert r["name"] == "Morning run" and len(r["points"]) == 3 and r["duration_s"] == 60
    assert 160 < r["distance_m"] < 180 and 9 < r["avg_kmh"] < 11          # ~170 m in 60 s -> ~10 km/h


def test_timezone_offsets_and_fractional_seconds_in_times():
    body = "<trk><trkseg>" + trkpt(1, 1, "2024-05-01T12:00:00.250+02:00") + trkpt(1, 1.001, "2024-05-01T10:01:00Z") + "</trkseg></trk>"
    r = parse_gpx(gpx(body))
    assert r["duration_s"] == 60


def test_multiple_segments_are_joined_and_duplicates_dropped():
    body = "<trk><trkseg>" + trkpt(1, 1) + trkpt(1, 1) + trkpt(1, 1.001) + "</trkseg><trkseg>" + trkpt(1.001, 1.001) + "</trkseg></trk>"
    r = parse_gpx(gpx(body))
    assert r["points"] == [[1, 1], [1, 1.001], [1.001, 1.001]] and r["avg_kmh"] is None


def test_falls_back_to_route_points_then_waypoints_and_ignores_namespace():
    rte = '<rte><rtept lat="2" lon="2"/><rtept lat="2" lon="2.001"/></rte>'
    assert len(parse_gpx(gpx(rte, ns=""))["points"]) == 2
    wpt = '<wpt lat="3" lon="3"/><wpt lat="3.001" lon="3"/>'
    assert parse_gpx(gpx(wpt))["points"] == [[3, 3], [3.001, 3]]


@pytest.mark.parametrize("data,hint", [
    (b"not xml at all", "valid GPX"),
    (b"<kml><a/></kml>", "GPX file"),
    (gpx(trkpt(1, 1)), "at least 2"),
    (gpx(trkpt(1, 1) + trkpt(1, 1)), "same place"),
    (gpx("<trk><trkseg>" + trkpt(95, 1) + trkpt(1, 1) + "</trkseg></trk>"), "coordinates"),
    (gpx('<trk><trkseg><trkpt lat="x" lon="1"/><trkpt lat="1" lon="1"/></trkseg></trk>'), "coordinates"),
    (b'<?xml version="1.0"?><!DOCTYPE g [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><gpx>&b;</gpx>', "valid GPX"),
])
def test_bad_files_get_friendly_errors(data, hint):
    with pytest.raises(AppError) as e:
        parse_gpx(data)
    assert e.value.code == "bad_gpx" and hint in (e.value.message + (e.value.hint or ""))


def test_size_limits():
    pts = "".join(trkpt(1 + i * 1e-5, 1) for i in range(MAX_POINTS + 1))
    with pytest.raises(AppError) as e:
        parse_gpx(gpx("<trk><trkseg>" + pts + "</trkseg></trk>"))
    assert "max" in e.value.message
    with pytest.raises(AppError) as e:
        parse_gpx(b"x" * (10 * 1024 * 1024 + 1))
    assert e.value.status == 413
