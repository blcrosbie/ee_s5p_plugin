"""Writing results out, including the v0.1 CSV column-alignment bug."""

from __future__ import annotations

import csv
import json

import pytest

from core import export

ROWS = [
    {"longitude": 1.0, "latitude": 2.0, "NO2": 0.5},
    {"longitude": 3.0, "latitude": 4.0, "NO2": 0.7},
]


class TestFieldNames:
    def test_union_in_first_seen_order(self):
        rows = [{"a": 1, "b": 2}, {"b": 3, "c": 4}]
        assert export.field_names(rows) == ["a", "b", "c"]

    def test_empty(self):
        assert export.field_names([]) == []


class TestWriteCsv:
    def test_round_trip(self, tmp_path):
        path = export.write_csv(ROWS, str(tmp_path / "out.csv"))
        with open(path, newline="", encoding="utf-8") as handle:
            read_back = list(csv.DictReader(handle))
        assert read_back[0]["NO2"] == "0.5"
        assert read_back[1]["longitude"] == "3.0"

    def test_ragged_rows_stay_in_their_columns(self, tmp_path):
        """v0.1 took the header from row 0 and wrote values positionally, so a
        row with a different key set silently landed under the wrong headings."""
        rows = [
            {"a": 1, "b": 2},
            {"b": 20, "c": 30},  # no "a", and an extra "c"
        ]
        path = export.write_csv(rows, str(tmp_path / "ragged.csv"))
        with open(path, newline="", encoding="utf-8") as handle:
            read_back = list(csv.DictReader(handle))
        assert read_back[0] == {"a": "1", "b": "2", "c": ""}
        assert read_back[1] == {"a": "", "b": "20", "c": "30"}

    def test_no_blank_lines_on_windows(self, tmp_path):
        path = export.write_csv(ROWS, str(tmp_path / "out.csv"))
        with open(path, "rb") as handle:
            assert b"\r\r\n" not in handle.read()

    def test_empty_input_is_an_error(self, tmp_path):
        with pytest.raises(export.ExportError):
            export.write_csv([], str(tmp_path / "x.csv"))

    def test_parent_directory_is_created(self, tmp_path):
        path = export.write_csv(ROWS, str(tmp_path / "deep" / "dir" / "out.csv"))
        assert path


class TestDetectLonLat:
    @pytest.mark.parametrize(
        "names, expected",
        [
            (["longitude", "latitude"], ("longitude", "latitude")),
            (["Longitude", "Latitude"], ("Longitude", "Latitude")),
            (["LON", "LAT"], ("LON", "LAT")),
            (["x", "y", "value"], ("x", "y")),
            (["long", "lat"], ("long", "lat")),
        ],
    )
    def test_detection_is_case_insensitive(self, names, expected):
        assert export.detect_lonlat_fields(names) == expected

    def test_longitude_wins_over_lon_when_both_present(self):
        assert export.detect_lonlat_fields(["lon", "lat", "longitude", "latitude"]) == (
            "longitude",
            "latitude",
        )

    def test_no_pair(self):
        assert export.detect_lonlat_fields(["a", "b"]) is None


class TestRowsToGeojson:
    def test_points_by_default(self):
        result = export.rows_to_geojson(ROWS)
        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) == 2
        assert result["features"][0]["geometry"] == {
            "type": "Point",
            "coordinates": [1.0, 2.0],
        }

    def test_resolution_makes_pixel_footprints(self):
        result = export.rows_to_geojson(ROWS, resolution_m=1000)
        geom = result["features"][0]["geometry"]
        assert geom["type"] == "Polygon"
        ring = geom["coordinates"][0]
        assert ring[0] == ring[-1]
        assert len(ring) == 5

    def test_properties_keep_every_column(self):
        result = export.rows_to_geojson(ROWS)
        assert set(result["features"][0]["properties"]) == {
            "longitude",
            "latitude",
            "NO2",
        }

    def test_blank_values_become_null(self):
        result = export.rows_to_geojson([{"longitude": 1, "latitude": 2, "v": ""}])
        assert result["features"][0]["properties"]["v"] is None

    def test_rows_without_coordinates_are_skipped_not_written_as_null(self):
        rows = [
            {"longitude": 1.0, "latitude": 2.0},
            {"longitude": None, "latitude": 4.0},
            {"longitude": "oops", "latitude": 4.0},
        ]
        result = export.rows_to_geojson(rows)
        assert len(result["features"]) == 1

    def test_all_rows_unusable_is_an_error(self):
        with pytest.raises(export.ExportError, match="usable"):
            export.rows_to_geojson([{"longitude": None, "latitude": None}])

    def test_missing_coordinate_columns_is_an_error(self):
        with pytest.raises(export.ExportError, match="No longitude/latitude"):
            export.rows_to_geojson([{"a": 1}])

    def test_empty_input_is_an_error(self):
        with pytest.raises(export.ExportError):
            export.rows_to_geojson([])


class TestWriteGeojson:
    def test_passes_through_a_feature_collection(self, tmp_path):
        payload = {"type": "FeatureCollection", "features": []}
        path = export.write_geojson(payload, str(tmp_path / "fc.geojson"))
        assert json.load(open(path, encoding="utf-8")) == payload

    def test_rejects_a_non_geojson_mapping(self, tmp_path):
        with pytest.raises(export.ExportError, match="Not a GeoJSON"):
            export.write_geojson({"type": "Nope"}, str(tmp_path / "x.geojson"))

    def test_converts_rows(self, tmp_path):
        path = export.write_geojson(ROWS, str(tmp_path / "rows.geojson"))
        assert len(json.load(open(path, encoding="utf-8"))["features"]) == 2


class TestNormalisePath:
    def test_adds_the_extension(self):
        assert export.normalise_path("out", ".csv") == "out.csv"

    def test_does_not_double_it_up(self):
        assert export.normalise_path("out.csv", ".csv") == "out.csv"

    def test_is_case_insensitive(self):
        assert export.normalise_path("out.CSV", ".csv") == "out.CSV"

    def test_accepts_an_extension_without_a_dot(self):
        assert export.normalise_path("out", "csv") == "out.csv"

    def test_empty_path_is_an_error(self):
        with pytest.raises(export.ExportError):
            export.normalise_path("", ".csv")


class TestSave:
    def test_dispatches_on_extension(self, tmp_path):
        assert export.save(ROWS, str(tmp_path / "a.csv")).endswith(".csv")
        assert export.save(ROWS, str(tmp_path / "a.json")).endswith(".json")
        assert export.save(ROWS, str(tmp_path / "a.geojson")).endswith(".geojson")

    def test_unsupported_extension(self, tmp_path):
        with pytest.raises(export.ExportError, match="Unsupported format"):
            export.save(ROWS, str(tmp_path / "a.shp"))

    def test_feature_collection_to_csv_is_refused_with_advice(self, tmp_path):
        with pytest.raises(export.ExportError, match="GeoJSON or JSON"):
            export.save(
                {"type": "FeatureCollection", "features": []},
                str(tmp_path / "a.csv"),
            )

    def test_resolution_is_honoured(self, tmp_path):
        path = export.save(ROWS, str(tmp_path / "a.geojson"), resolution_m=500)
        data = json.load(open(path, encoding="utf-8"))
        assert data["features"][0]["geometry"]["type"] == "Polygon"


class TestDialogHelpers:
    def test_filter_lists_every_format(self):
        text = export.dialog_filter()
        for extension in export.FORMATS:
            assert f"*{extension}" in text

    def test_extension_recovered_from_filter_entry(self):
        assert export.extension_from_filter("GeoJSON (*.geojson)") == ".geojson"
        assert export.extension_from_filter("Comma separated values (*.csv)") == ".csv"

    def test_unknown_filter_falls_back_to_the_first_format(self):
        assert export.extension_from_filter("whatever") == ".geojson"
