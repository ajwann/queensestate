"""WGS84 coordinate parsing and distance helpers."""

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

EARTH_RADIUS_MILES: Final = 3958.7613

# Mecklenburg County's extent as published in the portal catalog, padded by roughly 5 miles.
_SOUTH, _NORTH, _WEST, _EAST = 34.92, 35.60, -81.14, -80.52

_COORDINATES: Final = re.compile(
    r"^\s*\(?\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*\)?\s*$"
)


@dataclass(frozen=True, slots=True)
class Point:
    """A WGS84 location in decimal degrees."""

    latitude: float
    longitude: float

    def in_service_area(self) -> bool:
        """Return whether the point lies in (or just around) Mecklenburg County."""
        return _SOUTH <= self.latitude <= _NORTH and _WEST <= self.longitude <= _EAST


def parse_coordinates(text: str) -> Point | None:
    """Parse ``"latitude,longitude"`` (e.g. ``"35.2271,-80.8431"``); return None otherwise."""
    match = _COORDINATES.match(text)
    if match is None:
        return None
    latitude, longitude = float(match[1]), float(match[2])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return Point(latitude=latitude, longitude=longitude)


def haversine_miles(a: Point, b: Point) -> float:
    """Great-circle distance between two points in statute miles."""
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    half_chord = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(math.radians(b.longitude - a.longitude) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(half_chord))


def distance_to_geometry_miles(origin: Point, geometry: Mapping[str, Any] | None) -> float | None:
    """Approximate distance from ``origin`` to an Esri JSON geometry in WGS84.

    Points are exact. For lines and polygons the nearest vertex is used, which can slightly
    overstate the distance to a long straight segment. Returns None for empty geometry.
    """
    if not geometry:
        return None
    return min((haversine_miles(origin, vertex) for vertex in _vertices(geometry)), default=None)


def _vertices(geometry: Mapping[str, Any]) -> Iterator[Point]:
    x, y = geometry.get("x"), geometry.get("y")
    if isinstance(x, int | float) and isinstance(y, int | float):
        yield Point(latitude=float(y), longitude=float(x))
    parts = [*(geometry.get("paths") or ()), *(geometry.get("rings") or ())]
    if points := geometry.get("points"):
        parts.append(points)
    for part in parts:
        for vertex in part:
            if len(vertex) >= 2:
                yield Point(latitude=float(vertex[1]), longitude=float(vertex[0]))
