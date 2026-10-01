"""Pure geometry helpers: buffers around a point, areas, containment.

Kept free of Qt, QGIS and ``ee`` imports so it can be unit tested directly.
Coordinates are always ``(longitude, latitude)`` in WGS84 degrees, matching
GeoJSON and Earth Engine -- the original code mixed the two orders, which is how
a "radius" could silently come out transposed.
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius

#: Accepted length units, as a multiplier to kilometres.
UNITS_TO_KM = {
    "km": 1.0,
    "kilometre": 1.0,
    "kilometres": 1.0,
    "kilometer": 1.0,
    "kilometers": 1.0,
    "m": 1.0 / 1000.0,
    "metre": 1.0 / 1000.0,
    "metres": 1.0 / 1000.0,
    "meter": 1.0 / 1000.0,
    "meters": 1.0 / 1000.0,
    "mi": 1.609344,
    "mile": 1.609344,
    "miles": 1.609344,
    "ft": 0.0003048,
    "feet": 0.0003048,
    "foot": 0.0003048,
    "nmi": 1.852,
}


class GeometryError(ValueError):
    """Raised for coordinates or distances that cannot describe a shape."""


def to_km(distance: float, unit: str = "km") -> float:
    """Convert ``distance`` from ``unit`` to kilometres."""
    try:
        factor = UNITS_TO_KM[str(unit).strip().lower()]
    except KeyError:
        raise GeometryError(
            f"Unsupported unit {unit!r}; use one of {', '.join(sorted(set(UNITS_TO_KM)))}"
        ) from None
    return float(distance) * factor


def validate_lonlat(longitude: float, latitude: float) -> tuple[float, float]:
    """Range check a coordinate pair and return it as floats."""
    try:
        lon = float(longitude)
        lat = float(latitude)
    except (TypeError, ValueError):
        raise GeometryError(
            f"Coordinates must be numbers, got ({longitude!r}, {latitude!r})"
        ) from None
    if not -180.0 <= lon <= 180.0:
        raise GeometryError(f"Longitude {lon} is outside -180..180")
    if not -90.0 <= lat <= 90.0:
        raise GeometryError(f"Latitude {lat} is outside -90..90")
    return lon, lat


def destination(
    longitude: float, latitude: float, bearing_rad: float, distance_km: float
) -> tuple[float, float]:
    """Point reached by travelling ``distance_km`` along a great circle."""
    lon_rad = math.radians(longitude)
    lat_rad = math.radians(latitude)
    angular = distance_km / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat_rad) * math.cos(angular)
        + math.cos(lat_rad) * math.sin(angular) * math.cos(bearing_rad)
    )
    lon2 = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular) * math.cos(lat_rad),
        math.cos(angular) - math.sin(lat_rad) * math.sin(lat2),
    )
    # Keep longitude in -180..180 after crossing the antimeridian.
    lon_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return lon_deg, math.degrees(lat2)


def regular_polygon(
    longitude: float,
    latitude: float,
    radius: float = 1.0,
    unit: str = "km",
    sides: int = 6,
    close: bool = True,
) -> list[tuple[float, float]]:
    """A regular polygon centred on a point, as ``(lon, lat)`` pairs.

    ``radius`` is the circumradius, except for ``sides == 4``, where the shape is
    an axis-aligned square whose *side length* is ``radius``.  That special case
    exists so a pixel of a given ground resolution can be drawn by passing the
    resolution straight in; the original code attempted the same thing but halved
    an already-scaled radius, producing squares ~30% too small.

    The ring is closed by repeating the first vertex unless ``close`` is false,
    so the result is directly usable as a GeoJSON / Earth Engine linear ring.
    """
    lon, lat = validate_lonlat(longitude, latitude)
    sides = int(sides)
    if sides < 3:
        raise GeometryError(f"A polygon needs at least 3 sides, got {sides}")

    radius_km = to_km(radius, unit)
    if radius_km <= 0:
        raise GeometryError(f"Radius must be positive, got {radius} {unit}")

    step = 2.0 * math.pi / sides
    if sides == 4:
        # Half-diagonal of a square with side `radius`, corners at 45 degrees.
        radius_km *= math.sqrt(2.0) / 2.0
        start = step / 2.0
    else:
        start = 0.0

    ring = [
        destination(lon, lat, start + index * step, radius_km) for index in range(sides)
    ]
    if close:
        ring.append(ring[0])
    return ring


def bbox_polygon(bbox: list[float] | tuple[float, ...]) -> list[tuple[float, float]]:
    """``[west, south, east, north]`` -> a closed ``(lon, lat)`` ring."""
    if not bbox or len(bbox) < 4:
        raise GeometryError(f"Expected [west, south, east, north], got {bbox!r}")
    west, south, east, north = (float(v) for v in bbox[:4])
    return [
        (west, south),
        (east, south),
        (east, north),
        (west, north),
        (west, south),
    ]


def point_in_ring(
    longitude: float, latitude: float, ring: list[tuple[float, float]]
) -> bool:
    """Even-odd ray casting test. ``ring`` is ``(lon, lat)`` pairs."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    previous = count - 1
    for current in range(count):
        x1, y1 = ring[current][0], ring[current][1]
        x2, y2 = ring[previous][0], ring[previous][1]
        # The `y2 != y1` guard is the division the original left exposed.
        if (y1 > latitude) != (y2 > latitude) and y2 != y1:
            crossing = x1 + (x2 - x1) * (latitude - y1) / (y2 - y1)
            if longitude < crossing:
                inside = not inside
        previous = current
    return inside


def bbox_of(ring: list[tuple[float, float]]) -> list[float]:
    """``[west, south, east, north]`` covering a ring."""
    if not ring:
        raise GeometryError("Cannot take the extent of an empty ring")
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return [min(lons), min(lats), max(lons), max(lats)]


def bboxes_intersect(a: list[float], b: list[float]) -> bool:
    """Do two ``[west, south, east, north]`` boxes overlap?"""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return True  # An unknown extent must not filter anything out.
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def bbox_area_km2(bbox: list[float]) -> float:
    """Approximate area of a lon/lat box, accounting for latitude convergence."""
    if not bbox or len(bbox) < 4:
        return 0.0
    west, south, east, north = (float(v) for v in bbox[:4])
    if north < south:
        south, north = north, south
    degree_km = math.pi * EARTH_RADIUS_KM / 180.0
    height = (north - south) * degree_km
    mean_lat = math.radians((north + south) / 2.0)
    width = (east - west) * degree_km * math.cos(mean_lat)
    return abs(width * height)


def ring_to_geojson(ring: list[tuple[float, float]]) -> dict:
    """Wrap a closed ring as a GeoJSON Polygon geometry."""
    coordinates = [[float(x), float(y)] for x, y in ring]
    if coordinates and coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    return {"type": "Polygon", "coordinates": [coordinates]}
