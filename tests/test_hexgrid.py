"""Tiling an area of interest, with and without the optional ``h3`` package."""

from __future__ import annotations

import pytest

from core import hexgrid

# A box over western Pennsylvania, in GeoJSON (lon, lat) order.
BOX = {
    "type": "Polygon",
    "coordinates": [
        [[-80.5, 40.0], [-79.5, 40.0], [-79.5, 40.6], [-80.5, 40.6], [-80.5, 40.0]]
    ],
}

# Smaller than a single cell at any sensible resolution.
TINY = {
    "type": "Polygon",
    "coordinates": [
        [
            [-80.001, 40.0],
            [-80.0, 40.0],
            [-80.0, 40.001],
            [-80.001, 40.001],
            [-80.001, 40.0],
        ]
    ],
}

needs_h3 = pytest.mark.skipif(
    not hexgrid.h3_available(), reason="the optional h3 package is not installed"
)


@pytest.fixture
def no_h3(monkeypatch):
    """Force the fallback path, whether or not h3 is really installed."""
    monkeypatch.setattr(hexgrid, "_h3_module", None)
    monkeypatch.setattr(hexgrid, "_h3_checked", True)
    yield
    hexgrid.reset_h3_cache()


class TestAreaTable:
    def test_resolutions_0_to_15_are_present(self):
        assert set(hexgrid.H3_AVERAGE_AREA_KM2) == set(range(16))

    def test_each_step_is_about_seven_times_smaller(self):
        for res in range(1, 11):
            ratio = (
                hexgrid.H3_AVERAGE_AREA_KM2[res - 1] / hexgrid.H3_AVERAGE_AREA_KM2[res]
            )
            assert 6.5 < ratio < 7.5, f"res {res}: ratio {ratio}"

    @needs_h3
    def test_the_table_matches_the_real_library(self):
        """A hardcoded table that drifts from h3 would mis-cost every plan."""
        import h3

        for res, area in hexgrid.H3_AVERAGE_AREA_KM2.items():
            actual = h3.average_hexagon_area(res, unit="km^2")
            assert actual == pytest.approx(area, rel=1e-4), f"res {res}"


class TestCoarsestResolutionFor:
    def test_picks_the_coarsest_that_fits(self):
        # A res-5 cell is ~253 km2, res-4 is ~1770.
        assert hexgrid.coarsest_resolution_for(300) == 5
        assert hexgrid.coarsest_resolution_for(2000) == 4
        assert hexgrid.coarsest_resolution_for(1) == 8

    def test_respects_the_search_bounds(self):
        assert hexgrid.coarsest_resolution_for(1e9, minimum=3) == 3
        assert hexgrid.coarsest_resolution_for(300, minimum=6) == 6

    def test_returns_none_when_nothing_fits(self):
        assert hexgrid.coarsest_resolution_for(1e-9) is None
        assert hexgrid.coarsest_resolution_for(1, maximum=5) is None

    def test_non_positive_budget(self):
        assert hexgrid.coarsest_resolution_for(0) is None
        assert hexgrid.coarsest_resolution_for(-5) is None


class TestEstimateTileCount:
    def test_scales_with_area(self):
        one = hexgrid.estimate_tile_count(10_000, 5)
        two = hexgrid.estimate_tile_count(20_000, 5)
        assert two == pytest.approx(2 * one, rel=0.05)

    def test_at_least_one_tile_for_a_real_area(self):
        assert hexgrid.estimate_tile_count(0.001, 5) == 1

    def test_zero_area(self):
        assert hexgrid.estimate_tile_count(0, 5) == 0

    def test_unknown_resolution(self):
        assert hexgrid.estimate_tile_count(100, 99) == 0


class TestPolygonalParts:
    def test_polygon_passes_through(self):
        assert hexgrid.polygonal_parts(BOX) == [BOX]

    def test_feature_collection_is_flattened(self):
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": BOX, "properties": {}}],
        }
        assert hexgrid.polygonal_parts(collection) == [BOX]

    def test_non_polygonal_input_is_refused_clearly(self):
        """h3 reports this only as a bare ValueError."""
        with pytest.raises(hexgrid.HexGridError, match="must be a polygon"):
            hexgrid.polygonal_parts({"type": "Point", "coordinates": [0, 0]})

    def test_empty_input(self):
        with pytest.raises(hexgrid.HexGridError):
            hexgrid.polygonal_parts({})


class TestBboxOfGeojson:
    def test_polygon(self):
        assert hexgrid.bbox_of_geojson(BOX) == [-80.5, 40.0, -79.5, 40.6]

    def test_multipolygon_is_unioned(self):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                BOX["coordinates"],
                [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
            ],
        }
        assert hexgrid.bbox_of_geojson(multi) == [-80.5, 0.0, 1.0, 40.6]

    def test_feature_collection(self):
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": BOX, "properties": {}}],
        }
        assert hexgrid.bbox_of_geojson(collection) == [-80.5, 40.0, -79.5, 40.6]

    def test_nothing(self):
        assert hexgrid.bbox_of_geojson({}) is None


class TestFallbackGrid:
    """Must work whether or not h3 is installed."""

    def test_tiles_get_finer_as_resolution_rises(self, no_h3):
        grid = hexgrid.grid()
        assert grid.name == "grid"
        counts = [len(grid.tiles_for(BOX, res)) for res in (3, 4, 5, 6)]
        assert counts == sorted(counts), counts
        assert counts[-1] > counts[0]

    def test_tiles_cover_the_extent_without_gaps_or_overlaps(self, no_h3):
        """Exact coverage is the point: no missed ground, no duplicated samples."""
        from core import geometry

        grid = hexgrid.LatLonGrid()
        tiles = grid.tiles_for(BOX, 5)
        covered = sum(tile.area_km2 for tile in tiles)
        expected = geometry.bbox_area_km2(hexgrid.bbox_of_geojson(BOX))
        assert covered == pytest.approx(expected, rel=0.02)

    def test_rings_are_closed(self, no_h3):
        for tile in hexgrid.LatLonGrid().tiles_for(BOX, 5):
            assert tile.ring[0] == tile.ring[-1]

    def test_tiles_are_marked_as_not_indexed(self, no_h3):
        tile = hexgrid.LatLonGrid().tiles_for(BOX, 5)[0]
        assert tile.source == "grid"
        assert not tile.is_indexed
        assert not hexgrid.grid().indexed

    def test_describe_tells_the_user_what_they_are_missing(self, no_h3):
        assert "install h3" in hexgrid.grid().describe()

    def test_install_hint_is_offered_not_performed(self, no_h3):
        hint = hexgrid.install_hint()
        assert "pip install h3" in hint

    def test_area_with_no_extent_is_refused(self, no_h3):
        flat = {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]}
        with pytest.raises(hexgrid.HexGridError):
            hexgrid.LatLonGrid().tiles_for(flat, 5)


@needs_h3
class TestH3Grid:
    def test_grid_prefers_h3_when_available(self):
        grid = hexgrid.grid()
        assert grid.name == "h3"
        assert grid.indexed

    def test_tile_counts_follow_the_h3_hierarchy(self):
        grid = hexgrid.H3Grid()
        counts = {res: len(grid.tiles_for(BOX, res)) for res in (3, 4, 5, 6)}
        assert counts[3] < counts[4] < counts[5] < counts[6]
        # Each finer resolution divides a cell into about seven.
        assert 4 < counts[6] / counts[5] < 9

    def test_tile_ids_are_real_h3_indexes(self):
        import h3

        tile = hexgrid.H3Grid().tiles_for(BOX, 5)[0]
        assert tile.is_indexed
        assert h3.is_valid_cell(tile.id)
        assert h3.get_resolution(tile.id) == 5

    def test_boundaries_are_lon_lat_and_closed(self):
        """h3 4.x returns (lat, lng) unclosed; GeoJSON needs (lon, lat) closed."""
        tile = hexgrid.H3Grid().tiles_for(BOX, 5)[0]
        assert tile.ring[0] == tile.ring[-1]
        assert len(tile.ring) == 7  # six corners plus the repeat
        for lon, lat in tile.ring:
            assert -81 < lon < -79, f"longitude out of place: {lon}"
            assert 39 < lat < 42, f"latitude out of place: {lat}"

    def test_tile_areas_are_close_to_the_published_average(self):
        for tile in hexgrid.H3Grid().tiles_for(BOX, 5):
            assert tile.area_km2 == pytest.approx(hexgrid.H3_AVERAGE_AREA_KM2[5], rel=0.1)

    def test_multipolygon_areas_are_tiled(self):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                BOX["coordinates"],
                [
                    [
                        [-79.0, 41.0],
                        [-78.5, 41.0],
                        [-78.5, 41.3],
                        [-79.0, 41.3],
                        [-79.0, 41.0],
                    ]
                ],
            ],
        }
        single = len(hexgrid.H3Grid().tiles_for(BOX, 5))
        both = len(hexgrid.H3Grid().tiles_for(multi, 5))
        assert both > single

    def test_tiles_are_unique(self):
        tiles = hexgrid.H3Grid().tiles_for(BOX, 5)
        assert len({tile.id for tile in tiles}) == len(tiles)

    def test_holes_are_respected(self):
        outer = [[-81, 39.5], [-79, 39.5], [-79, 41], [-81, 41], [-81, 39.5]]
        inner = [
            [-80.4, 40.0],
            [-79.6, 40.0],
            [-79.6, 40.6],
            [-80.4, 40.6],
            [-80.4, 40.0],
        ]
        solid = {"type": "Polygon", "coordinates": [outer]}
        holed = {"type": "Polygon", "coordinates": [outer, inner]}
        grid = hexgrid.H3Grid()
        assert len(grid.tiles_for(holed, 5)) < len(grid.tiles_for(solid, 5))

    def test_a_point_is_refused_rather_than_crashing(self):
        with pytest.raises(hexgrid.HexGridError):
            hexgrid.H3Grid().tiles_for({"type": "Point", "coordinates": [0, 0]}, 5)


class TestTilesCovering:
    """The guard that stops a small area silently extracting nothing."""

    @needs_h3
    def test_an_area_smaller_than_one_cell_still_gets_a_tile(self):
        """h3 selects cells by centre containment, so this returns zero cells."""
        assert hexgrid.H3Grid().tiles_for(TINY, 5) == []
        tiles = hexgrid.tiles_covering(TINY, 5, hexgrid.H3Grid())
        assert len(tiles) == 1
        assert tiles[0].source == "aoi"

    def test_the_substitute_tile_covers_the_area(self):
        tiles = hexgrid.tiles_covering(TINY, 5, hexgrid.LatLonGrid())
        assert len(tiles) >= 1
        bbox = tiles[0].bbox
        wanted = hexgrid.bbox_of_geojson(TINY)
        assert bbox[0] == pytest.approx(wanted[0], abs=1e-6)
        assert bbox[2] == pytest.approx(wanted[2], abs=1e-6)

    def test_normal_areas_are_untouched(self, no_h3):
        tiles = hexgrid.tiles_covering(BOX, 5)
        assert len(tiles) > 1
        assert all(tile.source == "grid" for tile in tiles)


class TestChildrenOf:
    @needs_h3
    def test_h3_cells_have_seven_children_conserving_area(self):
        grid = hexgrid.H3Grid()
        parent = grid.tiles_for(BOX, 4)[0]
        children = hexgrid.children_of(parent, grid)
        assert len(children) in (5, 7)  # 5 around a pentagon
        assert all(child.resolution == 5 for child in children)
        assert sum(c.area_km2 for c in children) == pytest.approx(
            parent.area_km2, rel=0.01
        )

    @needs_h3
    def test_children_are_valid_h3_indexes(self):
        import h3

        grid = hexgrid.H3Grid()
        parent = grid.tiles_for(BOX, 4)[0]
        for child in hexgrid.children_of(parent, grid):
            assert h3.is_valid_cell(child.id)
            assert h3.cell_to_parent(child.id, 4) == parent.id

    def test_fallback_tiles_subdivide_too(self, no_h3):
        grid = hexgrid.LatLonGrid()
        parent = grid.tiles_for(BOX, 4)[0]
        children = hexgrid.children_of(parent, grid)
        assert len(children) > 1
        assert all(child.resolution == 5 for child in children)

    def test_the_finest_resolution_cannot_be_subdivided(self):
        tile = hexgrid.Tile(
            id="x",
            resolution=hexgrid.FINEST_RESOLUTION,
            ring=tuple(hexgrid.geom.bbox_polygon([0, 0, 1, 1])),
            area_km2=1.0,
            source="grid",
        )
        with pytest.raises(hexgrid.HexGridError, match="cannot be subdivided"):
            hexgrid.children_of(tile)


class TestTile:
    def test_bbox_and_geojson(self):
        tile = hexgrid.Tile(
            id="t",
            resolution=5,
            ring=tuple(hexgrid.geom.bbox_polygon([-1, -2, 3, 4])),
            area_km2=10.0,
        )
        assert tile.bbox == [-1.0, -2.0, 3.0, 4.0]
        geojson = tile.geojson()
        assert geojson["type"] == "Polygon"
        assert geojson["coordinates"][0][0] == geojson["coordinates"][0][-1]

    def test_total_area(self):
        tiles = [
            hexgrid.Tile("a", 5, ((0, 0),), 1.5),
            hexgrid.Tile("b", 5, ((0, 0),), 2.5),
        ]
        assert hexgrid.total_area_km2(tiles) == 4.0


def test_describe_resolution_is_human_readable():
    assert "km" in hexgrid.describe_resolution(5)
    assert "metropolitan" in hexgrid.describe_resolution(5)
    assert "m²" in hexgrid.describe_resolution(12)


def test_resolution_table_covers_the_range():
    table = hexgrid.resolution_table(3, 8)
    assert [res for res, _ in table] == [3, 4, 5, 6, 7, 8]
    areas = [area for _, area in table]
    assert areas == sorted(areas, reverse=True)
