"""Planning a chunked extraction: sizing, auto-scaling and refinement."""

from __future__ import annotations

import pytest

from core import hexgrid
from core import plan as plan_mod
from core import request as request_mod
from core.catalog import Dataset

# A box over Pennsylvania, roughly 139,000 km2.
STATE = {
    "type": "Polygon",
    "coordinates": [
        [
            [-80.52, 39.72],
            [-74.69, 39.72],
            [-74.69, 42.27],
            [-80.52, 42.27],
            [-80.52, 39.72],
        ]
    ],
}

CITY = {
    "type": "Polygon",
    "coordinates": [
        [
            [-80.10, 40.40],
            [-79.90, 40.40],
            [-79.90, 40.50],
            [-80.10, 40.50],
            [-80.10, 40.40],
        ]
    ],
}

S5P_BANDS = (
    "NO2_column_number_density",
    "tropospheric_NO2_column_number_density",
    "cloud_fraction",
)


def s5p(**overrides) -> Dataset:
    raw = {
        "id": "COPERNICUS/S5P/OFFL/L3_NO2",
        "title": "Sentinel-5P OFFL NO2",
        "type": "ImageCollection",
        "provider": "COPERNICUS",
        "start": "2018-06-28T00:00:00Z",
        "ongoing": True,
        "bbox": [-180, -90, 180, 90],
        "gsd": 1113.2,
        "interval": {"type": "cadence", "unit": "day", "interval": 1},
        "bands": [{"name": name} for name in S5P_BANDS],
    }
    raw.update(overrides)
    return Dataset.from_dict(raw)


def table(**overrides) -> Dataset:
    raw = {
        "id": "TIGER/2018/States",
        "title": "US states",
        "type": "FeatureCollection",
        "provider": "TIGER",
        "bbox": [-180, -90, 180, 90],
    }
    raw.update(overrides)
    return Dataset.from_dict(raw)


@pytest.fixture
def grid():
    """Use the deterministic fallback grid so counts do not depend on h3."""
    return hexgrid.LatLonGrid()


# ---------------------------------------------------------------------------
# Sizing arithmetic
# ---------------------------------------------------------------------------


class TestTileAreaBudget:
    def test_budget_scales_with_the_square_of_resolution(self):
        """Pixel count falls as the square of pixel size, so area budget rises."""
        coarse = plan_mod.tile_area_budget_km2(2000, bands=1, images=1)
        fine = plan_mod.tile_area_budget_km2(1000, bands=1, images=1)
        assert coarse == pytest.approx(4 * fine, rel=1e-6)

    def test_budget_falls_with_bands_and_images(self):
        one = plan_mod.tile_area_budget_km2(1000, bands=1, images=1)
        assert plan_mod.tile_area_budget_km2(1000, bands=4, images=1) == pytest.approx(
            one / 4
        )
        assert plan_mod.tile_area_budget_km2(1000, bands=1, images=10) == pytest.approx(
            one / 10
        )

    def test_budget_keeps_a_safety_margin(self):
        budget = plan_mod.tile_area_budget_km2(1000, 1, 1)
        at_limit = request_mod.VALUE_LIMIT * 1.0
        assert budget < at_limit

    def test_a_budget_actually_fits(self):
        """The arithmetic has to agree with the estimator it is derived from."""
        budget = plan_mod.tile_area_budget_km2(1113.2, bands=3, images=15)
        estimate = request_mod.estimate_image_collection(
            plan_mod._square_bbox(budget), 1113.2, bands=3, images=15
        )
        assert estimate.fits, estimate.describe()

    def test_zero_resolution_is_refused(self):
        with pytest.raises(plan_mod.PlanError):
            plan_mod.tile_area_budget_km2(0, 1, 1)


class TestChooseTileResolution:
    def test_a_light_request_tiles_coarsely(self):
        resolution, _ = plan_mod.choose_tile_resolution(1113.2, bands=1, images=15)
        assert resolution == 3

    def test_a_heavy_request_tiles_finely(self):
        resolution, _ = plan_mod.choose_tile_resolution(1113.2, bands=12, images=183)
        assert resolution is not None
        assert resolution >= 5

    def test_finer_ground_resolution_needs_finer_tiles(self):
        coarse, _ = plan_mod.choose_tile_resolution(1000, bands=1, images=30)
        fine, _ = plan_mod.choose_tile_resolution(100, bands=1, images=30)
        assert fine > coarse

    def test_returns_none_when_nothing_in_range_fits(self):
        resolution, _ = plan_mod.choose_tile_resolution(
            10, bands=36, images=5000, maximum=6
        )
        assert resolution is None


class TestRequiredResolution:
    def test_tells_the_user_what_would_work(self):
        area = hexgrid.H3_AVERAGE_AREA_KM2[9]
        needed = plan_mod.required_resolution_m(area, bands=36, images=5000)
        estimate = request_mod.estimate_image_collection(
            plan_mod._square_bbox(area), needed, bands=36, images=5000
        )
        assert estimate.fits, estimate.describe()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestWholeAreaShortCircuit:
    def test_a_small_request_is_not_tiled_at_all(self, grid):
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert not built.chunked
        assert built.tile_resolution is None
        assert built.tile_count == 1
        assert "one request" in built.describe()

    def test_the_single_chunk_covers_the_area(self, grid):
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        chunk = built.chunks()[0]
        assert chunk.request.bbox == pytest.approx(
            tuple(hexgrid.bbox_of_geojson(CITY)), rel=1e-9
        )

    def test_force_tiling_overrides_the_short_circuit(self, grid):
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=grid,
            force_tiling=True,
        )
        assert built.tile_resolution is not None


class TestAutoScale:
    def test_a_state_over_a_month_tiles(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert built.chunked
        assert built.tile_count > 1
        assert built.tile_resolution >= hexgrid.DEFAULT_MIN_RESOLUTION

    def test_a_heavier_request_tiles_more_finely(self, grid):
        light = plan_mod.plan(
            s5p(),
            STATE,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        heavy = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-01-01",
            "2025-01-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert heavy.tile_resolution > light.tile_resolution
        assert heavy.tile_count > light.tile_count

    def test_every_planned_tile_fits_within_the_limit(self, grid):
        """The whole point: no tile should be refused as too large."""
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-01-01",
            "2025-01-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        images = built.images
        for tile in built.tiles:
            estimate = request_mod.estimate_image_collection(
                tile.bbox, built.resolution_m, bands=len(built.bands), images=images
            )
            assert estimate.fits, f"{tile.id}: {estimate.describe()}"

    def test_the_plan_reports_the_per_tile_estimate(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert built.per_tile is not None
        assert built.per_tile.values > 0
        assert built.total_values >= built.per_tile.values

    def test_search_bounds_are_respected(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
            minimum_resolution=6,
            maximum_resolution=9,
        )
        assert built.tile_resolution >= 6


class TestPlanRefusals:
    def test_too_many_tiles_is_refused_with_advice(self, grid):
        """A plan that would fit, but only as more requests than allowed."""
        with pytest.raises(plan_mod.PlanError, match="requests"):
            plan_mod.plan(
                s5p(),
                STATE,
                S5P_BANDS,
                "2024-06-01",
                "2024-07-01",
                resolution_m=1113.2,
                tile_grid=grid,
                max_tiles=20,
            )

    def test_the_tile_count_refusal_says_what_to_change(self, grid):
        with pytest.raises(plan_mod.PlanError) as caught:
            plan_mod.plan(
                s5p(),
                STATE,
                S5P_BANDS,
                "2024-06-01",
                "2024-07-01",
                resolution_m=1113.2,
                tile_grid=grid,
                max_tiles=20,
            )
        message = str(caught.value)
        assert "coarser ground resolution" in message or "shorter date range" in message

    def test_an_impossible_request_names_a_resolution_that_would_work(self, grid):
        with pytest.raises(plan_mod.PlanError) as caught:
            plan_mod.plan(
                s5p(),
                STATE,
                S5P_BANDS,
                "2018-07-01",
                "2026-01-01",
                resolution_m=1,
                tile_grid=grid,
                maximum_resolution=5,
            )
        message = str(caught.value)
        assert "ground resolution of about" in message

    def test_no_bands_is_refused(self, grid):
        with pytest.raises(plan_mod.PlanError, match="band"):
            plan_mod.plan(
                s5p(),
                STATE,
                (),
                "2024-06-01",
                "2024-07-01",
                resolution_m=1113.2,
                tile_grid=grid,
            )

    def test_no_resolution_is_refused(self, grid):
        with pytest.raises(plan_mod.PlanError, match="resolution"):
            plan_mod.plan(
                s5p(),
                STATE,
                S5P_BANDS,
                "2024-06-01",
                "2024-07-01",
                resolution_m=0,
                tile_grid=grid,
            )

    def test_an_area_with_no_coordinates_is_refused(self, grid):
        with pytest.raises(plan_mod.PlanError, match="no coordinates"):
            plan_mod.plan(
                s5p(),
                {},
                S5P_BANDS,
                "2024-06-01",
                "2024-07-01",
                resolution_m=1113.2,
                tile_grid=grid,
            )


class TestTablePlans:
    def test_a_small_area_is_one_request(self, grid):
        built = plan_mod.plan(
            table(), CITY, (), None, None, resolution_m=0, tile_grid=grid
        )
        assert not built.chunked
        assert built.tile_count == 1

    def test_a_large_area_is_tiled_by_area(self, grid):
        built = plan_mod.plan(
            table(), STATE, (), None, None, resolution_m=0, tile_grid=grid
        )
        assert built.chunked
        assert built.tile_count > 1

    def test_the_feature_limit_is_explained(self, grid):
        built = plan_mod.plan(
            table(), STATE, (), None, None, resolution_m=0, tile_grid=grid
        )
        assert any("5,000 features" in note for note in built.notes)

    def test_tables_need_no_bands_or_resolution(self, grid):
        built = plan_mod.plan(
            table(), CITY, (), None, None, resolution_m=0, tile_grid=grid
        )
        assert built.chunks()[0].request.dataset_type == "FeatureCollection"


class TestFallbackNote:
    def test_the_user_is_told_when_ids_are_not_real_h3(self, grid):
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert any("h3 is not installed" in note for note in built.notes)
        assert not built.indexed

    @pytest.mark.skipif(not hexgrid.h3_available(), reason="h3 not installed")
    def test_no_note_when_h3_is_present(self):
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=hexgrid.H3Grid(),
        )
        assert not any("h3 is not installed" in note for note in built.notes)
        assert built.indexed


class TestTiming:
    def test_more_workers_predicts_less_time(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-01-01",
            "2025-01-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert built.seconds(4) < built.seconds(2) < built.seconds(1)

    def test_speedup_reaches_the_promised_band(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-01-01",
            "2025-01-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert built.speedup(4) > 2.0

    def test_timing_options_cover_every_choice(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        options = built.timing_options([1, 2, 4])
        assert [workers for workers, _, _ in options] == [1, 2, 4]
        assert options[0][2] == pytest.approx(1.0)

    def test_describe_mentions_the_speedup_when_parallel(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert "faster" in built.describe(workers=4)
        assert "faster" not in built.describe(workers=1)


class TestChunks:
    def test_one_chunk_per_tile(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        chunks = built.chunks()
        assert len(chunks) == built.tile_count
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_chunks_carry_the_request_parameters(self, grid):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        chunk = built.chunks()[0]
        assert chunk.request.dataset_id == "COPERNICUS/S5P/OFFL/L3_NO2"
        assert chunk.request.bands == S5P_BANDS
        assert chunk.request.resolution_m == 1113.2
        assert chunk.request.bbox == tuple(chunk.tile.bbox)

    def test_region_is_left_for_the_caller(self, grid):
        """core never imports ee, so the ee.Geometry is built in the gui layer."""
        built = plan_mod.plan(
            s5p(),
            CITY,
            ("cloud_fraction",),
            "2024-06-01",
            "2024-06-02",
            resolution_m=1113.2,
            tile_grid=grid,
        )
        assert built.chunks()[0].request.region is None


class TestSubdivide:
    def _chunk(self, grid, resolution=4):
        built = plan_mod.plan(
            s5p(),
            STATE,
            S5P_BANDS,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
            minimum_resolution=resolution,
            maximum_resolution=resolution,
        )
        return built.chunks()[0]

    def test_a_rejected_chunk_becomes_several_finer_ones(self, grid):
        chunk = self._chunk(grid)
        children = plan_mod.subdivide(chunk, grid)
        assert len(children) > 1
        assert all(c.tile.resolution == chunk.tile.resolution + 1 for c in children)

    def test_children_keep_the_request_parameters(self, grid):
        chunk = self._chunk(grid)
        for child in plan_mod.subdivide(chunk, grid):
            assert child.request.dataset_id == chunk.request.dataset_id
            assert child.request.bands == chunk.request.bands
            assert child.request.start == chunk.request.start
            assert child.request.bbox == tuple(child.tile.bbox)

    def test_children_are_smaller_than_the_parent(self, grid):
        chunk = self._chunk(grid)
        for child in plan_mod.subdivide(chunk, grid):
            assert child.tile.area_km2 < chunk.tile.area_km2

    def test_indices_continue_from_the_offset(self, grid):
        children = plan_mod.subdivide(self._chunk(grid), grid, start_index=100)
        assert [c.index for c in children][:2] == [100, 101]

    def test_the_finest_tile_cannot_be_subdivided(self, grid):
        chunk = self._chunk(grid)
        finest = hexgrid.Tile(
            id="x",
            resolution=hexgrid.FINEST_RESOLUTION,
            ring=chunk.tile.ring,
            area_km2=chunk.tile.area_km2,
            source="grid",
        )
        assert plan_mod.subdivide(plan_mod.Chunk(0, finest, chunk.request), grid) == []

    @pytest.mark.skipif(not hexgrid.h3_available(), reason="h3 not installed")
    def test_an_aoi_tile_that_cannot_shrink_stops_refining(self):
        """Otherwise refinement would loop forever on a tiny area."""
        tiny = {
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
        tile = hexgrid.tiles_covering(tiny, 9, hexgrid.H3Grid())[0]
        chunk = plan_mod.Chunk(
            0,
            tile,
            request_mod.ExtractRequest(
                dataset_id="x",
                dataset_type="ImageCollection",
                bands=("b",),
                start="2024-01-01",
                end="2024-01-02",
                resolution_m=10,
                bbox=tuple(tile.bbox),
            ),
        )
        assert plan_mod.subdivide(chunk, hexgrid.H3Grid()) == []


class TestTagRows:
    def test_h3_tiles_stamp_an_h3_index(self):
        tile = hexgrid.Tile("8a2a1072b59ffff", 10, ((0, 0),), 1.0, source="h3")
        rows = plan_mod.tag_rows([{"value": 1}], tile)
        assert rows[0]["h3_index"] == "8a2a1072b59ffff"
        assert rows[0]["tile_resolution"] == 10

    def test_fallback_tiles_stamp_a_plain_tile_id(self):
        """Not calling it h3_index matters: the id is not a real H3 cell."""
        tile = hexgrid.Tile("grid:5/0/0", 5, ((0, 0),), 1.0, source="grid")
        rows = plan_mod.tag_rows([{"value": 1}], tile)
        assert rows[0]["tile_id"] == "grid:5/0/0"
        assert "h3_index" not in rows[0]

    def test_every_row_is_tagged(self):
        tile = hexgrid.Tile("abc", 7, ((0, 0),), 1.0, source="h3")
        rows = plan_mod.tag_rows([{"v": i} for i in range(5)], tile)
        assert all(row["h3_index"] == "abc" for row in rows)

    def test_tagged_rows_still_export(self, tmp_path):
        """The H3 index has to survive into the file for the join to be possible."""
        import json

        from core import export

        tile = hexgrid.Tile("8a2a1072b59ffff", 10, ((0, 0),), 1.0, source="h3")
        rows = plan_mod.tag_rows([{"longitude": 1.0, "latitude": 2.0, "NO2": 0.5}], tile)
        path = export.save(rows, str(tmp_path / "out.geojson"))
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["features"][0]["properties"]["h3_index"] == "8a2a1072b59ffff"
