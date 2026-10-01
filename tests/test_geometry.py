"""Geometry helpers, including the square-footprint sizing bug from v0.1."""

from __future__ import annotations

import math

import pytest

from core import geometry


def test_unit_conversion():
    assert geometry.to_km(1, "km") == pytest.approx(1.0)
    assert geometry.to_km(1000, "m") == pytest.approx(1.0)
    assert geometry.to_km(1, "mi") == pytest.approx(1.609344)
    assert geometry.to_km(1, "MILES") == pytest.approx(1.609344)
    assert geometry.to_km(5280, "ft") == pytest.approx(1.609344, rel=1e-3)


def test_unknown_unit_is_rejected_loudly():
    """The original printed a note and silently used the raw number."""
    with pytest.raises(geometry.GeometryError):
        geometry.to_km(1, "furlongs")


@pytest.mark.parametrize(
    "lon, lat",
    [(181, 0), (-181, 0), (0, 91), (0, -91), ("x", 0), (None, 0)],
)
def test_invalid_coordinates_are_rejected(lon, lat):
    with pytest.raises(geometry.GeometryError):
        geometry.validate_lonlat(lon, lat)


def test_destination_moves_north():
    lon, lat = geometry.destination(0.0, 0.0, 0.0, 111.195)
    assert lon == pytest.approx(0.0, abs=1e-9)
    assert lat == pytest.approx(1.0, abs=1e-3)


def test_destination_wraps_the_antimeridian():
    lon, _ = geometry.destination(179.9, 0.0, math.pi / 2, 50.0)
    assert -180.0 <= lon <= 180.0
    assert lon < 0  # crossed into the western hemisphere


class TestRegularPolygon:
    def test_hexagon_is_closed_and_has_seven_points(self):
        ring = geometry.regular_polygon(10.0, 50.0, radius=5, unit="km", sides=6)
        assert len(ring) == 7
        assert ring[0] == ring[-1]

    def test_hexagon_vertices_sit_on_the_circumradius(self):
        ring = geometry.regular_polygon(0.0, 0.0, radius=10, unit="km", sides=6)
        degree_km = math.pi * geometry.EARTH_RADIUS_KM / 180.0
        for lon, lat in ring[:-1]:
            distance = math.hypot(lon * degree_km, lat * degree_km)
            assert distance == pytest.approx(10.0, rel=0.01)

    def test_square_side_equals_the_requested_resolution(self):
        """A 1000 m pixel must produce a 1000 m square, not a 700 m one.

        v0.1 scaled the radius by sqrt(2) and *then* halved it, while also
        treating the result as a circumradius, so footprints came out ~30% small.
        """
        ring = geometry.regular_polygon(0.0, 0.0, radius=1000, unit="m", sides=4)
        bbox = geometry.bbox_of(ring)
        degree_km = math.pi * geometry.EARTH_RADIUS_KM / 180.0
        width_m = (bbox[2] - bbox[0]) * degree_km * 1000
        height_m = (bbox[3] - bbox[1]) * degree_km * 1000
        assert width_m == pytest.approx(1000.0, rel=0.01)
        assert height_m == pytest.approx(1000.0, rel=0.01)

    def test_square_area_matches_the_pixel_it_represents(self):
        ring = geometry.regular_polygon(0.0, 0.0, radius=1113.2, unit="m", sides=4)
        area = geometry.bbox_area_km2(geometry.bbox_of(ring))
        assert area == pytest.approx((1.1132) ** 2, rel=0.02)

    @pytest.mark.parametrize("sides", [0, 1, 2, -3])
    def test_too_few_sides_is_rejected(self, sides):
        with pytest.raises(geometry.GeometryError):
            geometry.regular_polygon(0.0, 0.0, sides=sides)

    @pytest.mark.parametrize("radius", [0, -1])
    def test_non_positive_radius_is_rejected(self, radius):
        with pytest.raises(geometry.GeometryError):
            geometry.regular_polygon(0.0, 0.0, radius=radius)

    def test_open_ring_when_requested(self):
        ring = geometry.regular_polygon(0.0, 0.0, sides=5, close=False)
        assert len(ring) == 5
        assert ring[0] != ring[-1]


class TestPointInRing:
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]

    def test_inside(self):
        assert geometry.point_in_ring(1.0, 1.0, self.square)

    def test_outside(self):
        assert not geometry.point_in_ring(3.0, 1.0, self.square)
        assert not geometry.point_in_ring(1.0, 3.0, self.square)

    def test_degenerate_ring_is_not_a_crash(self):
        """v0.1 divided by zero on a ring with repeated latitudes."""
        assert not geometry.point_in_ring(1.0, 1.0, [(0.0, 0.0), (1.0, 0.0)])
        assert not geometry.point_in_ring(1.0, 0.0, [(0.0, 0.0), (2.0, 0.0), (0.0, 0.0)])


def test_bbox_polygon_round_trip():
    ring = geometry.bbox_polygon([-10, -5, 10, 5])
    assert geometry.bbox_of(ring) == [-10.0, -5.0, 10.0, 5.0]
    assert ring[0] == ring[-1]


def test_bbox_polygon_rejects_short_input():
    with pytest.raises(geometry.GeometryError):
        geometry.bbox_polygon([1, 2])


class TestBboxIntersect:
    def test_overlapping(self):
        assert geometry.bboxes_intersect([0, 0, 10, 10], [5, 5, 15, 15])

    def test_disjoint(self):
        assert not geometry.bboxes_intersect([0, 0, 10, 10], [20, 20, 30, 30])

    def test_touching_counts(self):
        assert geometry.bboxes_intersect([0, 0, 10, 10], [10, 10, 20, 20])

    def test_unknown_extent_never_excludes(self):
        assert geometry.bboxes_intersect([0, 0, 10, 10], None)
        assert geometry.bboxes_intersect(None, [0, 0, 10, 10])


def test_bbox_area_accounts_for_latitude():
    """A box at 60N covers about half the ground area of the same box at 0."""
    equator = geometry.bbox_area_km2([0, 0, 1, 1])
    north = geometry.bbox_area_km2([0, 59.5, 1, 60.5])
    assert north == pytest.approx(equator * math.cos(math.radians(60)), rel=0.02)


def test_bbox_area_of_whole_earth_is_plausible():
    area = geometry.bbox_area_km2([-180, -90, 180, 90])
    assert 4e8 < area < 1e9  # true surface is ~5.1e8 km2


def test_ring_to_geojson_closes_the_ring():
    geom = geometry.ring_to_geojson([(0, 0), (1, 0), (1, 1)])
    assert geom["type"] == "Polygon"
    assert geom["coordinates"][0][0] == geom["coordinates"][0][-1]
