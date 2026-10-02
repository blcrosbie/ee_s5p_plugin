"""Normalising STAC collection records into the plugin's dataset shape.

These run against fixtures rather than the network; ``test_live.py`` covers the
real catalog and is opt-in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import stac

S5P_RECORD = {
    "type": "Collection",
    "stac_version": "1.0.0",
    "id": "COPERNICUS/S5P/OFFL/L3_NO2",
    "title": "Sentinel-5P OFFL NO2: Offline Nitrogen Dioxide",
    "gee:type": "image_collection",
    "gee:status": "ready",
    "description": "Sentinel-5 Precursor is a satellite ... [terms](http://x) <sub>2</sub>",
    "keywords": ["No2", "pollution", "TROPOMI"],
    "gee:categories": ["atmosphere"],
    "license": "proprietary",
    "gee:terms_of_use": "Copernicus terms",
    "gee:interval": {"type": "cadence", "unit": "day", "interval": 1},
    "providers": [
        {"name": "European Union/ESA/Copernicus", "roles": ["licensor", "producer"]},
        {"name": "Google Earth Engine", "roles": ["host"], "url": "http://g"},
    ],
    "extent": {
        "spatial": {"bbox": [[-180, -90, 180, 90]]},
        "temporal": {"interval": [["2018-06-28T10:24:07Z", "2026-09-20T18:16:05Z"]]},
    },
    "summaries": {
        "gsd": [1113.2],
        "eo:bands": [
            {
                "name": "NO2_column_number_density",
                "description": "Total vertical column of NO<sub>2</sub>",
                "gee:units": "mol/m^2",
                "gsd": [1113.2],
            },
            {"name": "cloud_fraction", "gee:units": "fraction"},
        ],
        "NO2_column_number_density": {"minimum": -0.0005, "maximum": 0.0192},
        "gee:visualizations": [
            {
                "display_name": "RGB",
                "image_visualization": {
                    "band_vis": {
                        "bands": ["tropospheric_NO2_column_number_density"],
                        "min": [0],
                        "max": [0.0002],
                        "palette": ["black", "blue", "red"],
                    }
                },
            }
        ],
    },
    "links": [{"rel": "self", "href": "http://stac/x.json"}],
}


@pytest.fixture
def s5p():
    return stac.normalise_dataset(S5P_RECORD, stac_url="http://stac/x.json")


class TestNormaliseDataset:
    def test_identity_and_type_mapping(self, s5p):
        assert s5p["id"] == "COPERNICUS/S5P/OFFL/L3_NO2"
        assert s5p["type"] == "ImageCollection"
        assert s5p["gee_type"] == "image_collection"
        assert s5p["provider"] == "COPERNICUS"

    def test_publisher_prefers_producer_over_the_google_host(self, s5p):
        assert s5p["publisher"] == "European Union/ESA/Copernicus"

    def test_tags_are_lowercased_and_sorted(self, s5p):
        assert s5p["tags"] == ["no2", "pollution", "tropomi"]

    def test_temporal_extent(self, s5p):
        assert s5p["start"] == "2018-06-28T10:24:07Z"
        assert s5p["end"] == "2026-09-20T18:16:05Z"

    def test_spatial_extent(self, s5p):
        assert s5p["bbox"] == [-180, -90, 180, 90]

    def test_bands_carry_units_and_ranges(self, s5p):
        bands = {b["name"]: b for b in s5p["bands"]}
        assert bands["NO2_column_number_density"]["units"] == "mol/m^2"
        assert bands["NO2_column_number_density"]["gsd"] == 1113.2
        assert bands["NO2_column_number_density"]["maximum"] == 0.0192
        assert "minimum" not in bands["cloud_fraction"]

    def test_visualisation_scalars_are_unwrapped(self, s5p):
        viz = s5p["visualizations"][0]
        assert viz["kind"] == "image"
        assert viz["min"] == 0 and viz["max"] == 0.0002
        assert viz["palette"] == ["black", "blue", "red"]

    def test_doc_url_uses_the_underscore_slug(self, s5p):
        assert s5p["doc_url"].endswith("COPERNICUS_S5P_OFFL_L3_NO2")

    def test_empty_values_are_dropped_to_keep_the_snapshot_small(self, s5p):
        assert "citation" not in s5p
        assert "version" not in s5p


class TestNormaliseRejects:
    def test_bigquery_tables_are_skipped(self):
        """They cannot be opened through the ee Python API at all."""
        record = dict(S5P_RECORD, **{"gee:type": "bigquery_table"})
        assert stac.normalise_dataset(record) is None

    def test_unknown_type_is_skipped(self):
        record = dict(S5P_RECORD, **{"gee:type": "something_new"})
        assert stac.normalise_dataset(record) is None

    def test_catalogs_are_skipped(self):
        assert stac.normalise_dataset(dict(S5P_RECORD, type="Catalog")) is None

    def test_record_without_id_is_skipped(self):
        record = {k: v for k, v in S5P_RECORD.items() if k != "id"}
        assert stac.normalise_dataset(record) is None


class TestTemporalEdgeCases:
    def test_open_ended_interval_yields_no_end(self):
        record = dict(S5P_RECORD)
        record["extent"] = {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [["2018-01-01T00:00:00Z", None]]},
        }
        assert "end" not in stac.normalise_dataset(record)

    def test_multiple_intervals_are_spanned(self):
        record = dict(S5P_RECORD)
        record["extent"] = {
            "spatial": {
                "bbox": [[0, 0, 1, 1]],
            },
            "temporal": {
                "interval": [
                    ["2000-01-01T00:00:00Z", "2001-01-01T00:00:00Z"],
                    ["2010-01-01T00:00:00Z", "2011-01-01T00:00:00Z"],
                ]
            },
        }
        result = stac.normalise_dataset(record)
        assert result["start"] == "2000-01-01T00:00:00Z"
        assert result["end"] == "2011-01-01T00:00:00Z"

    def test_several_bboxes_are_unioned(self):
        record = dict(S5P_RECORD)
        record["extent"] = {
            "spatial": {"bbox": [[0, 0, 10, 10], [-5, -5, 2, 2]]},
            "temporal": {"interval": [["2000-01-01T00:00:00Z", None]]},
        }
        assert stac.normalise_dataset(record)["bbox"] == [-5, -5, 10, 10]


class TestDeprecation:
    def test_status_deprecated_sets_the_flag(self):
        record = dict(S5P_RECORD, **{"gee:status": "deprecated"})
        assert stac.normalise_dataset(record)["deprecated"] is True

    def test_deprecated_key_sets_the_flag(self):
        record = dict(S5P_RECORD, deprecated=True)
        assert stac.normalise_dataset(record)["deprecated"] is True

    def test_ready_datasets_are_not_flagged(self, s5p):
        assert "deprecated" not in s5p


class TestMarkOngoing:
    """Google publishes the newest ingested item as the end of the extent."""

    def _dataset(self, days_old, **extra):
        end = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(days=days_old)
        return dict({"id": "x", "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}, **extra)

    def test_recent_end_date_means_still_growing(self):
        datasets = [self._dataset(3)]
        stac.mark_ongoing(datasets, reference=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert datasets[0]["ongoing"] is True

    def test_old_end_date_means_closed(self):
        datasets = [self._dataset(900)]
        stac.mark_ongoing(datasets, reference=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert "ongoing" not in datasets[0]

    def test_deprecated_is_never_ongoing_however_recent(self):
        datasets = [self._dataset(1, deprecated=True)]
        stac.mark_ongoing(datasets, reference=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert "ongoing" not in datasets[0]

    def test_missing_end_date_is_ongoing(self):
        datasets = [{"id": "x"}]
        stac.mark_ongoing(datasets)
        assert datasets[0]["ongoing"] is True


def test_build_snapshot_header():
    snapshot = stac.build_snapshot([dict(S5P_RECORD, id="a", end=None)])
    assert snapshot["schema_version"] == stac.SCHEMA_VERSION
    assert snapshot["count"] == 1
    assert snapshot["ongoing"] == 1
    assert snapshot["generated"].endswith("Z")


def test_summarise_counts_by_type():
    datasets = [{"type": "Image"}, {"type": "ImageCollection"}, {"type": "Image"}]
    assert stac.summarise(datasets) == {"Image": 2, "ImageCollection": 1}


class TestPlainText:
    def test_markdown_links_keep_their_label(self):
        assert stac.plain_text("see [the terms](http://x) now") == "see the terms now"

    def test_html_is_stripped_and_whitespace_collapsed(self):
        assert stac.plain_text("NO<sub>2</sub>\n\n  level") == "NO2 level"

    def test_limit_adds_an_ellipsis(self):
        assert stac.plain_text("abcdefghij", limit=5) == "abcd…"

    def test_empty_input(self):
        assert stac.plain_text(None) == ""


def test_dataset_slug():
    assert stac.dataset_slug("COPERNICUS/S5P/OFFL/L3_NO2") == "COPERNICUS_S5P_OFFL_L3_NO2"


class TestCrawl:
    """The crawl walks catalog -> provider -> dataset using a stubbed client."""

    class FakeClient:
        def __init__(self, pages):
            self.pages = pages
            self.requested = []

        def get_json(self, url):
            self.requested.append(url)
            if url not in self.pages:
                raise stac.StacError(f"missing {url}")
            return self.pages[url]

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _pages(self):
        return {
            "root": {
                "links": [
                    {"rel": "child", "href": "prov_a"},
                    {"rel": "child", "href": "prov_b"},
                    {"rel": "self", "href": "root"},
                ]
            },
            "prov_a": {"links": [{"rel": "child", "href": "ds1"}]},
            "prov_b": {
                "links": [
                    {"rel": "child", "href": "ds2"},
                    {"rel": "child", "href": "ds1"},  # cross-linked
                ]
            },
            "ds1": dict(S5P_RECORD, id="A/one"),
            "ds2": dict(S5P_RECORD, id="B/two"),
        }

    def test_dataset_urls_are_deduplicated(self):
        client = self.FakeClient(self._pages())
        urls = stac.dataset_urls("root", client=client, workers=2)
        assert urls == ["ds1", "ds2"]

    def test_unreachable_provider_does_not_abort_the_crawl(self):
        pages = self._pages()
        del pages["prov_b"]
        client = self.FakeClient(pages)
        assert stac.dataset_urls("root", client=client, workers=2) == ["ds1"]

    def test_empty_root_is_an_error(self):
        client = self.FakeClient({"root": {"links": []}})
        with pytest.raises(stac.StacError):
            stac.dataset_urls("root", client=client)

    def test_progress_callback_can_cancel(self):
        client = self.FakeClient(self._pages())
        calls = []

        def progress(done, total, message):
            calls.append((done, total))
            return False  # cancel immediately

        assert stac.dataset_urls("root", client=client, progress=progress) == []
        assert calls


class TestUrlSchemeGuard:
    """The crawl follows links out of the catalog, so URLs are not all ours.

    Without this, a redirected or tampered catalog could point at file:/// and
    have the plugin read local files back as dataset metadata -- urlopen honours
    those schemes happily. plugins.qgis.org's Bandit scan flags the unguarded
    call (B310) and blocks the upload over it.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://storage.googleapis.com/earthengine-stac/catalog/catalog.json",
            "http://example.org/catalog.json",
        ],
    )
    def test_web_urls_are_allowed(self, url):
        assert stac._require_web_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file:///C:/Windows/win.ini",
            "ftp://host/catalog.json",
            "javascript:alert(1)",
            "data:application/json,{}",
            "catalog.json",
            "",
        ],
    )
    def test_everything_else_is_refused(self, url):
        with pytest.raises(stac.StacError, match="only https and http"):
            stac._require_web_url(url)

    def test_the_guard_runs_before_any_fetch(self, monkeypatch):
        """It has to reject before the request is built, not after."""
        client = stac.HttpClient(retries=1)
        monkeypatch.setattr(
            client, "_session", None
        )  # force the urllib path, which is the flagged one
        with pytest.raises(stac.StacError, match="Refusing to fetch"):
            client._get_json_once("file:///etc/passwd")


class TestHttpClientRetries:
    def test_retries_then_raises_stac_error(self, monkeypatch):
        client = stac.HttpClient(retries=3)
        attempts = []

        def boom(url):
            attempts.append(url)
            raise OSError("network down")

        monkeypatch.setattr(client, "_get_json_once", boom)
        with pytest.raises(stac.StacError, match="network down"):
            client.get_json("http://x")
        assert len(attempts) == 3

    def test_succeeds_on_a_later_attempt(self, monkeypatch):
        client = stac.HttpClient(retries=3)
        state = {"n": 0}

        def flaky(url):
            state["n"] += 1
            if state["n"] < 2:
                raise OSError("blip")
            return {"ok": True}

        monkeypatch.setattr(client, "_get_json_once", flaky)
        assert client.get_json("http://x") == {"ok": True}
