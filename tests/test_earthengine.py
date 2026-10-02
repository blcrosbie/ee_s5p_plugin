"""The Earth Engine adapter, with ``ee`` stubbed out.

``core.earthengine`` is the only module that touches ``ee``, and it imports it
lazily through :func:`require_ee`.  That makes it testable: a fake ``ee`` module
injected into :data:`sys.modules` exercises the geometry bridging and the
response parsing -- which is where the real logic lives -- without credentials or
a network.
"""

from __future__ import annotations

import sys
import types

import pytest

from core import earthengine as ee_mod
from core.request import ExtractRequest

# ---------------------------------------------------------------------------
# A fake ee module
# ---------------------------------------------------------------------------


class FakeGeometry:
    """Records how it was constructed so assertions can inspect it."""

    def __init__(self, spec, kind="Geometry"):
        self.spec = spec
        self.kind = kind

    def __repr__(self):
        return f"FakeGeometry({self.kind}, {self.spec!r})"


def _make_fake_ee(initialised: bool = True) -> types.ModuleType:
    module = types.ModuleType("ee")

    # earthengine-api exposes ee.data.is_initialized(); the plugin checks it
    # before building any ee object, because constructing even a Geometry
    # contacts the server to load the API signatures.
    data = types.ModuleType("ee.data")
    data.is_initialized = lambda: initialised
    module.data = data

    class Geometry(FakeGeometry):
        def __init__(self, spec):
            super().__init__(spec, kind=(spec or {}).get("type", "Geometry"))

        @staticmethod
        def Polygon(coordinates):
            return FakeGeometry(coordinates, kind="Polygon")

        @staticmethod
        def MultiPolygon(coordinates):
            return FakeGeometry(coordinates, kind="MultiPolygon")

        @staticmethod
        def Rectangle(bounds):
            return FakeGeometry(bounds, kind="Rectangle")

    module.Geometry = Geometry
    return module


@pytest.fixture
def fake_ee(monkeypatch):
    module = _make_fake_ee()
    monkeypatch.setitem(sys.modules, "ee", module)
    monkeypatch.setattr(ee_mod, "_ee", None)
    yield module
    monkeypatch.setattr(ee_mod, "_ee", None)


@pytest.fixture
def uninitialised_ee(monkeypatch):
    """`ee` imports, but nobody ever signed in.

    This is the real situation when the Earth Engine plugin fails inside its
    classFactory: its bundled `ee` is on sys.path and imports cleanly, so an
    import check passes and the first actual call blows up deep inside ee.
    """
    module = _make_fake_ee(initialised=False)
    monkeypatch.setitem(sys.modules, "ee", module)
    monkeypatch.setattr(ee_mod, "_ee", None)
    yield module
    monkeypatch.setattr(ee_mod, "_ee", None)


@pytest.fixture
def no_ee(monkeypatch):
    """Simulate the Earth Engine plugin not being installed."""
    monkeypatch.setattr(ee_mod, "_ee", None)
    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__
    )

    def blocked(name, *args, **kwargs):
        if name == "ee":
            raise ImportError("No module named 'ee'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "ee", None)
    monkeypatch.setattr("builtins.__import__", blocked)
    yield
    monkeypatch.setattr(ee_mod, "_ee", None)


class TestAvailability:
    def test_missing_ee_explains_where_to_get_it(self, no_ee):
        with pytest.raises(ee_mod.EarthEngineUnavailable) as caught:
            ee_mod.require_ee()
        message = str(caught.value)
        assert ee_mod.EE_PLUGIN_NAME in message
        assert "restart QGIS" in message

    def test_is_available_is_false_without_ee(self, no_ee):
        assert ee_mod.is_available() is False

    def test_is_available_is_true_with_ee(self, fake_ee):
        assert ee_mod.is_available() is True


# ---------------------------------------------------------------------------
# Geometry bridging
# ---------------------------------------------------------------------------


class TestNotSignedIn:
    """Importable is not the same as signed in."""

    def test_is_available_is_true_but_is_initialised_is_false(self, uninitialised_ee):
        assert ee_mod.is_available() is True
        assert ee_mod.is_initialised() is False

    def test_is_initialised_is_true_when_signed_in(self, fake_ee):
        assert ee_mod.is_initialised() is True

    @pytest.mark.parametrize(
        "call",
        [
            lambda: ee_mod.bbox_to_ee([0, 0, 1, 1]),
            lambda: ee_mod.ring_to_ee([(0, 0), (1, 0), (1, 1)]),
            lambda: ee_mod.geojson_to_ee(POLYGON),
        ],
        ids=["bbox_to_ee", "ring_to_ee", "geojson_to_ee"],
    )
    def test_building_geometry_is_refused_with_advice(self, uninitialised_ee, call):
        """v1.0.2 let ee's own exception escape as a traceback in the QGIS log."""
        with pytest.raises(ee_mod.EarthEngineNotInitialised) as caught:
            call()
        message = str(caught.value)
        assert "ee.Authenticate()" in message
        assert "Python Console" in message

    def test_the_advice_warns_the_project_will_be_asked_for_again(self):
        """ee.Authenticate() rewrites the credentials file from scratch.

        The Earth Engine plugin stores the Cloud project id in that same file, so
        authenticating silently clears it. Advice that did not say so would send
        people straight into the next failure.
        """
        advice = ee_mod.NOT_INITIALISED_ADVICE
        assert "Cloud" in advice and "project" in advice
        assert "restart qgis" in advice.lower()

    def test_the_advice_is_actionable(self):
        advice = ee_mod.NOT_INITIALISED_ADVICE
        for fragment in (
            "ee.Authenticate()",
            "Python Console",
            "auth_mode='notebook'",
            "credentials",
        ):
            assert fragment in advice, f"advice no longer mentions {fragment!r}"

    def test_it_is_distinguishable_from_a_missing_install(self):
        """Different remedy, so it must be catchable separately."""
        assert issubclass(ee_mod.EarthEngineNotInitialised, ee_mod.EarthEngineUnavailable)
        assert ee_mod.EarthEngineNotInitialised is not ee_mod.EarthEngineUnavailable

    def test_older_ee_without_the_predicate_falls_back(self, monkeypatch):
        """earthengine-api before 1.x has no ee.data.is_initialized."""
        module = _make_fake_ee()
        del module.data.is_initialized

        calls = []

        class Number:
            def __init__(self, value):
                self.value = value

            def getInfo(self):
                calls.append(self.value)
                return self.value

        module.Number = Number
        monkeypatch.setitem(sys.modules, "ee", module)
        monkeypatch.setattr(ee_mod, "_ee", None)
        assert ee_mod.is_initialised() is True
        assert calls, "should have fallen back to a server round trip"
        monkeypatch.setattr(ee_mod, "_ee", None)


class TestGeometryBridging:
    def test_ring_is_closed_before_being_sent(self, fake_ee):
        geometry = ee_mod.ring_to_ee([(0, 0), (1, 0), (1, 1)])
        ring = geometry.spec[0]
        assert ring[0] == ring[-1]

    def test_already_closed_ring_is_not_closed_twice(self, fake_ee):
        geometry = ee_mod.ring_to_ee([(0, 0), (1, 0), (1, 1), (0, 0)])
        assert len(geometry.spec[0]) == 4

    def test_degenerate_ring_is_refused(self, fake_ee):
        with pytest.raises(ee_mod.EarthEngineError, match="three distinct"):
            ee_mod.ring_to_ee([(0, 0), (1, 1)])

    def test_bbox_to_rectangle(self, fake_ee):
        geometry = ee_mod.bbox_to_ee([-10, -5, 10, 5])
        assert geometry.kind == "Rectangle"
        assert geometry.spec == [-10.0, -5.0, 10.0, 5.0]

    def test_short_bbox_is_refused(self, fake_ee):
        with pytest.raises(ee_mod.EarthEngineError):
            ee_mod.bbox_to_ee([1, 2])


POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
OTHER_POLYGON = {"type": "Polygon", "coordinates": [[[5, 5], [6, 5], [6, 6], [5, 5]]]}
POINT = {"type": "Point", "coordinates": [1, 2]}


class TestGeojsonToEe:
    def test_a_bare_geometry_passes_through(self, fake_ee):
        assert ee_mod.geojson_to_ee(POLYGON).kind == "Polygon"

    def test_a_feature_is_unwrapped(self, fake_ee):
        feature = {"type": "Feature", "geometry": POLYGON, "properties": {}}
        assert ee_mod.geojson_to_ee(feature).kind == "Polygon"

    def test_a_single_feature_collection_is_unwrapped(self, fake_ee):
        collection = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": POLYGON, "properties": {}}],
        }
        assert ee_mod.geojson_to_ee(collection).kind == "Polygon"

    def test_several_polygons_merge_into_one_multipolygon(self, fake_ee):
        """Done on the GeoJSON, so building an area of interest costs no
        server round trips."""
        collection = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": POLYGON, "properties": {}},
                {"type": "Feature", "geometry": OTHER_POLYGON, "properties": {}},
            ],
        }
        geometry = ee_mod.geojson_to_ee(collection)
        assert geometry.kind == "MultiPolygon"
        assert len(geometry.spec) == 2

    def test_multipolygon_parts_are_flattened_in(self, fake_ee):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [POLYGON["coordinates"], OTHER_POLYGON["coordinates"]],
        }
        collection = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": multi, "properties": {}},
                {"type": "Feature", "geometry": POLYGON, "properties": {}},
            ],
        }
        geometry = ee_mod.geojson_to_ee(collection)
        assert geometry.kind == "MultiPolygon"
        assert len(geometry.spec) == 3

    def test_mixed_geometries_fall_back_to_a_collection(self, fake_ee):
        collection = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": POLYGON, "properties": {}},
                {"type": "Feature", "geometry": POINT, "properties": {}},
            ],
        }
        assert ee_mod.geojson_to_ee(collection).kind == "GeometryCollection"

    def test_nested_geometry_collections_are_flattened(self, fake_ee):
        nested = {
            "type": "GeometryCollection",
            "geometries": [
                POLYGON,
                {"type": "GeometryCollection", "geometries": [OTHER_POLYGON]},
            ],
        }
        geometry = ee_mod.geojson_to_ee(nested)
        assert geometry.kind == "MultiPolygon"
        assert len(geometry.spec) == 2

    def test_empty_collection_is_refused(self, fake_ee):
        with pytest.raises(ee_mod.EarthEngineError, match="no geometries"):
            ee_mod.geojson_to_ee({"type": "FeatureCollection", "features": []})

    def test_non_mapping_is_refused(self, fake_ee):
        with pytest.raises(ee_mod.EarthEngineError):
            ee_mod.geojson_to_ee([1, 2, 3])


# ---------------------------------------------------------------------------
# Parsing responses
# ---------------------------------------------------------------------------


class TestBandNames:
    def test_from_an_image_payload(self):
        info = {"bands": [{"id": "B1"}, {"id": "B2"}]}
        assert ee_mod.band_names(info) == ["B1", "B2"]

    def test_from_a_collection_payload(self):
        info = {"features": [{"bands": [{"id": "NO2"}]}]}
        assert ee_mod.band_names(info) == ["NO2"]

    def test_entries_without_an_id_are_skipped(self):
        info = {"bands": [{"id": "B1"}, {"crs": "EPSG:4326"}, "junk"]}
        assert ee_mod.band_names(info) == ["B1"]

    def test_empty_and_malformed_payloads(self):
        assert ee_mod.band_names({}) == []
        assert ee_mod.band_names({"features": []}) == []
        assert ee_mod.band_names(None) == []


class TestBandCrs:
    def test_found_on_an_image(self):
        info = {"bands": [{"id": "B1", "crs": "EPSG:32617"}]}
        assert ee_mod.band_crs(info, "B1") == "EPSG:32617"

    def test_found_on_a_collection(self):
        info = {"features": [{"bands": [{"id": "B1", "crs": "EPSG:4326"}]}]}
        assert ee_mod.band_crs(info, "B1") == "EPSG:4326"

    def test_missing_band(self):
        assert ee_mod.band_crs({"bands": [{"id": "B1"}]}, "B2") is None


class TestFootprintBbox:
    def test_from_a_system_footprint_ring(self):
        info = {
            "features": [
                {
                    "properties": {
                        "system:footprint": {
                            "type": "LinearRing",
                            "coordinates": [[0, 0], [10, 0], [10, 5], [0, 5]],
                        }
                    }
                }
            ]
        }
        assert ee_mod.footprint_bbox(info) == [0.0, 0.0, 10.0, 5.0]

    def test_from_nested_rings(self):
        info = {
            "features": [
                {
                    "properties": {
                        "system:footprint": {
                            "coordinates": [[[0, 0], [2, 2]], [[-5, -5], [1, 1]]]
                        }
                    }
                }
            ]
        }
        assert ee_mod.footprint_bbox(info) == [-5.0, -5.0, 2.0, 2.0]

    def test_from_explicit_corner_properties(self):
        """Some collections publish LAT_MIN/LON_MIN rather than a footprint."""
        info = {
            "features": [
                {
                    "properties": {
                        "LON_MIN": -20,
                        "LAT_MIN": -10,
                        "LON_MAX": 20,
                        "LAT_MAX": 10,
                    }
                }
            ]
        }
        assert ee_mod.footprint_bbox(info) == [-20.0, -10.0, 20.0, 10.0]

    def test_several_features_are_unioned(self):
        def feature(bounds):
            return {
                "properties": {
                    "LON_MIN": bounds[0],
                    "LAT_MIN": bounds[1],
                    "LON_MAX": bounds[2],
                    "LAT_MAX": bounds[3],
                }
            }

        info = {"features": [feature([0, 0, 5, 5]), feature([-3, 2, 4, 9])]}
        assert ee_mod.footprint_bbox(info) == [-3.0, 0.0, 5.0, 9.0]

    def test_no_geometry_information_returns_none(self):
        assert ee_mod.footprint_bbox({"features": [{"properties": {}}]}) is None
        assert ee_mod.footprint_bbox({}) is None


class TestRegionTableToRows:
    """``getRegion`` returns a header row then value rows."""

    request = ExtractRequest(
        dataset_id="COPERNICUS/S5P/OFFL/L3_NO2",
        dataset_type="ImageCollection",
        bands=("NO2_column_number_density",),
        start="2020-01-01",
        end="2020-01-02",
        resolution_m=1113.2,
        crs="EPSG:4326",
        bbox=(-1, -1, 1, 1),
    )

    TABLE = [
        ["id", "longitude", "latitude", "time", "NO2_column_number_density"],
        ["20200101T101112_20200101T115959", -0.5, 0.5, 1577873472000, 0.00012],
        ["20200102T101112_20200102T115959", -0.4, 0.6, 1577959872000, 0.00015],
    ]

    def test_columns_are_named(self):
        rows = ee_mod.region_table_to_rows(self.TABLE, self.request)
        assert len(rows) == 2
        assert rows[0]["longitude"] == -0.5
        assert rows[0]["NO2_column_number_density"] == 0.00012

    def test_image_timestamps_are_expanded(self):
        rows = ee_mod.region_table_to_rows(self.TABLE, self.request)
        assert rows[0]["image_start"] == "2020-01-01 10:11:12"
        assert rows[0]["image_stop"] == "2020-01-01 11:59:59"

    def test_epoch_time_is_made_readable(self):
        rows = ee_mod.region_table_to_rows(self.TABLE, self.request)
        assert rows[0]["time_utc"].startswith("2020-01-01")

    def test_rows_record_the_request_so_the_file_is_self_describing(self):
        rows = ee_mod.region_table_to_rows(self.TABLE, self.request)
        row = rows[0]
        assert row["dataset_id"] == "COPERNICUS/S5P/OFFL/L3_NO2"
        assert row["resolution_m"] == 1113.2
        assert row["crs"] == "EPSG:4326"
        assert row["filter_start"] == "2020-01-01"
        assert row["filter_bands"] == "NO2_column_number_density"

    def test_an_unparseable_id_does_not_break_the_row(self):
        table = [self.TABLE[0], ["weird-id", 0, 0, None, 1.0]]
        rows = ee_mod.region_table_to_rows(table, self.request)
        assert rows[0]["id"] == "weird-id"
        assert "image_start" not in rows[0]

    def test_header_only_and_empty_tables(self):
        assert ee_mod.region_table_to_rows([self.TABLE[0]], self.request) == []
        assert ee_mod.region_table_to_rows([], self.request) == []
        assert ee_mod.region_table_to_rows(None, self.request) == []

    def test_rows_are_exportable_as_geojson(self, tmp_path):
        """The two halves have to fit together: getRegion output -> a map layer."""
        from core import export

        rows = ee_mod.region_table_to_rows(self.TABLE, self.request)
        path = export.save(rows, str(tmp_path / "out.geojson"), resolution_m=1113.2)
        import json

        data = json.loads(open(path, encoding="utf-8").read())
        assert len(data["features"]) == 2
        assert data["features"][0]["geometry"]["type"] == "Polygon"
