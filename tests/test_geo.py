import pytest

from queensestate.geo import (
    Point,
    distance_to_geometry_miles,
    haversine_miles,
    parse_coordinates,
)

UPTOWN = Point(latitude=35.2271, longitude=-80.8431)
RALEIGH = Point(latitude=35.7796, longitude=-78.6382)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("35.2271,-80.8431", UPTOWN),
        ("  (35.2271 , -80.8431) ", UPTOWN),
        ("35,-80", Point(35.0, -80.0)),
    ],
)
def test_parse_coordinates_accepts_lat_lon(text: str, expected: Point) -> None:
    assert parse_coordinates(text) == expected


@pytest.mark.parametrize(
    "text", ["600 E 4th St", "35.2271 -80.8431", "95.0,-80.0", "35.0,-181.0", ""]
)
def test_parse_coordinates_rejects_non_coordinates(text: str) -> None:
    assert parse_coordinates(text) is None


def test_service_area_covers_charlotte_but_not_raleigh() -> None:
    assert UPTOWN.in_service_area()
    assert not RALEIGH.in_service_area()


def test_haversine_matches_known_distance() -> None:
    assert haversine_miles(UPTOWN, RALEIGH) == pytest.approx(130, abs=3)
    assert haversine_miles(UPTOWN, UPTOWN) == 0


def test_distance_to_point_line_and_empty_geometry() -> None:
    point = {"x": UPTOWN.longitude, "y": UPTOWN.latitude}
    line = {"paths": [[[-80.90, 35.30], [UPTOWN.longitude, UPTOWN.latitude + 0.01]]]}
    assert distance_to_geometry_miles(UPTOWN, point) == 0
    assert distance_to_geometry_miles(UPTOWN, line) == pytest.approx(0.69, abs=0.01)
    assert distance_to_geometry_miles(UPTOWN, None) is None
    assert distance_to_geometry_miles(UPTOWN, {"paths": []}) is None
