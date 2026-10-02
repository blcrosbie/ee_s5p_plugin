"""The in-memory catalog: dataset records, indexes and filtering."""

from __future__ import annotations

import pytest

from core.catalog import Band, Catalog, Dataset, Filters, Visualisation

RAW = {
    "id": "COPERNICUS/S5P/OFFL/L3_NO2",
    "title": "Sentinel-5P OFFL NO2",
    "type": "ImageCollection",
    "provider": "COPERNICUS",
    "publisher": "ESA",
    "description": "Nitrogen dioxide from the TROPOMI instrument.",
    "tags": ["no2", "pollution", "tropomi"],
    "start": "2018-06-28T10:24:07Z",
    "end": "2026-09-20T18:16:05Z",
    "ongoing": True,
    "bbox": [-180, -90, 180, 90],
    "gsd": 1113.2,
    "interval": {"type": "cadence", "unit": "day", "interval": 1},
    "bands": [
        {"name": "NO2_column_number_density", "units": "mol/m^2", "gsd": 1113.2},
        {"name": "cloud_fraction"},
    ],
    "visualizations": [
        {
            "name": "RGB",
            "kind": "image",
            "bands": ["NO2_column_number_density"],
            "min": 0,
            "max": 0.0002,
            "palette": ["black", "red"],
        }
    ],
}


def make(**overrides) -> Dataset:
    return Dataset.from_dict({**RAW, **overrides})


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        [
            make(),
            make(
                id="LANDSAT/LC09/C02/T1_L2",
                title="Landsat 9",
                provider="LANDSAT",
                tags=["landsat", "usgs", "sr"],
                publisher="USGS",
                start="2021-10-31T00:00:00Z",
                end=None,
                ongoing=True,
                bbox=[-180, -90, 180, 90],
            ),
            make(
                id="OLD/DATASET",
                title="Retired thing",
                provider="OLD",
                tags=["pollution"],
                deprecated=True,
                ongoing=False,
                start="2000-01-01T00:00:00Z",
                end="2005-01-01T00:00:00Z",
            ),
            make(
                id="EU/REGIONS",
                title="Regions",
                type="FeatureCollection",
                provider="EU",
                tags=["borders"],
                bands=[],
                visualizations=[],
                ongoing=False,
                start="2015-01-01T00:00:00Z",
                end="2016-01-01T00:00:00Z",
                bbox=[-10, 35, 30, 70],
            ),
        ],
        generated="2026-10-01T00:00:00Z",
    )


class TestDataset:
    def test_bands_and_names(self):
        dataset = make()
        assert dataset.band_names == ("NO2_column_number_density", "cloud_fraction")
        assert isinstance(dataset.bands[0], Band)
        assert dataset.band("cloud_fraction").units is None
        assert dataset.band("nope") is None

    def test_band_label_includes_units(self):
        assert make().bands[0].label == "NO2_column_number_density (mol/m^2)"

    def test_visualisation_lookup(self):
        dataset = make()
        assert isinstance(dataset.visualizations[0], Visualisation)
        viz = dataset.visualisation_for("NO2_column_number_density")
        assert viz.palette == ("black", "red")
        assert dataset.visualisation_for("cloud_fraction") is None

    def test_ongoing_collection_reports_no_hard_end(self):
        dataset = make()
        assert dataset.effective_end is None
        assert dataset.availability.endswith("present")

    def test_closed_collection_keeps_its_end(self):
        dataset = make(ongoing=False)
        assert dataset.effective_end == "2026-09-20T18:16:05Z"

    def test_cadence_is_readable(self):
        assert make().cadence == "every 1 day"
        assert (
            make(interval={"type": "cadence", "unit": "day", "interval": 8}).cadence
            == "every 8 days"
        )
        assert (
            make(
                interval={"type": "revisit_interval", "unit": "day", "interval": 16}
            ).cadence
            == "revisit every 16 days"
        )
        assert make(interval=None).cadence is None

    def test_default_resolution_falls_back_to_band_gsd(self):
        assert make().default_resolution() == 1113.2
        assert make(gsd=None).default_resolution() == 1113.2
        assert make(gsd=None, bands=[{"name": "b"}]).default_resolution() is None

    def test_is_global(self):
        assert make().is_global
        assert not make(bbox=[-10, 35, 30, 70]).is_global
        assert not make(bbox=None).is_global

    def test_to_dict_round_trip(self):
        dataset = make()
        again = Dataset.from_dict(dataset.to_dict())
        assert again.id == dataset.id
        assert again.tags == dataset.tags
        assert again.ongoing == dataset.ongoing


class TestTextSearch:
    def test_matches_id_title_tag_band_and_publisher(self):
        dataset = make()
        for query in ["s5p", "sentinel", "tropomi", "cloud_fraction", "esa", "NO2"]:
            assert dataset.matches_text(query), query

    def test_all_terms_must_match(self):
        dataset = make()
        assert dataset.matches_text("sentinel no2")
        assert not dataset.matches_text("sentinel landsat")

    def test_empty_query_matches_everything(self):
        assert make().matches_text("")

    def test_search_reaches_into_the_description(self):
        assert make().matches_text("instrument")


class TestFilters:
    def test_empty_filters_hide_only_deprecated(self, catalog):
        results = catalog.search(Filters())
        assert len(results) == 3
        assert all(not d.deprecated for d in results)

    def test_deprecated_can_be_included(self, catalog):
        assert len(catalog.search(Filters(include_deprecated=True))) == 4

    def test_filter_by_type(self, catalog):
        results = catalog.search(Filters(types=("FeatureCollection",)))
        assert [d.id for d in results] == ["EU/REGIONS"]

    def test_filter_by_provider(self, catalog):
        results = catalog.search(Filters(providers=("LANDSAT",)))
        assert [d.id for d in results] == ["LANDSAT/LC09/C02/T1_L2"]

    def test_tags_or_is_the_default(self, catalog):
        results = catalog.search(Filters(tags=("no2", "landsat")))
        assert len(results) == 2

    def test_tags_and_requires_all(self, catalog):
        assert catalog.search(Filters(tags=("no2", "landsat"), tags_match_all=True)) == []
        results = catalog.search(Filters(tags=("no2", "tropomi"), tags_match_all=True))
        assert len(results) == 1

    def test_bands_only_excludes_tables(self, catalog):
        results = catalog.search(Filters(bands_only=True))
        assert "EU/REGIONS" not in [d.id for d in results]

    def test_text_filter(self, catalog):
        results = catalog.search(Filters(text="landsat"))
        assert [d.id for d in results] == ["LANDSAT/LC09/C02/T1_L2"]

    def test_bbox_filter_excludes_non_overlapping(self, catalog):
        # A box over Australia misses the Europe-only feature collection.
        results = catalog.search(
            Filters(bbox=(110, -45, 155, -10), types=("FeatureCollection",))
        )
        assert results == []

    def test_bbox_filter_keeps_overlapping(self, catalog):
        results = catalog.search(
            Filters(bbox=(0, 40, 10, 50), types=("FeatureCollection",))
        )
        assert [d.id for d in results] == ["EU/REGIONS"]

    def test_time_filter_uses_effective_end_for_ongoing(self, catalog):
        """An ongoing collection must still match a window past its snapshot end."""
        results = catalog.search(Filters(start="2030-01-01", end="2030-06-01"))
        assert "COPERNICUS/S5P/OFFL/L3_NO2" in [d.id for d in results]

    def test_time_filter_excludes_a_closed_dataset(self, catalog):
        results = catalog.search(
            Filters(start="2030-01-01", end="2030-06-01", types=("FeatureCollection",))
        )
        assert results == []

    def test_filters_combine(self, catalog):
        results = catalog.search(Filters(types=("ImageCollection",), tags=("pollution",)))
        assert [d.id for d in results] == ["COPERNICUS/S5P/OFFL/L3_NO2"]

    def test_is_empty(self):
        assert Filters().is_empty()
        assert not Filters(text="x").is_empty()
        assert not Filters(include_deprecated=True).is_empty()


class TestCatalogIndexes:
    def test_types_use_the_preferred_display_order(self, catalog):
        assert catalog.types() == ["ImageCollection", "FeatureCollection"]

    def test_providers_are_sorted(self, catalog):
        assert catalog.providers() == ["COPERNICUS", "EU", "LANDSAT", "OLD"]

    def test_publishers(self, catalog):
        assert catalog.publishers() == ["ESA", "USGS"]

    def test_tags_can_be_scoped_to_a_result_set(self, catalog):
        subset = catalog.search(Filters(providers=("LANDSAT",)))
        assert catalog.tags(subset) == ["landsat", "sr", "usgs"]
        assert len(catalog.tags()) > 3

    def test_tag_counts_are_ordered_by_frequency(self, catalog):
        counts = catalog.tag_counts()
        assert counts["pollution"] == 2
        assert next(iter(counts)) == "pollution"

    def test_counts_by_type_and_deprecated(self, catalog):
        assert catalog.counts_by_type() == {"ImageCollection": 3, "FeatureCollection": 1}
        assert catalog.deprecated_count == 1

    def test_container_protocol(self, catalog):
        assert len(catalog) == 4
        assert "EU/REGIONS" in catalog
        assert "nope" not in catalog
        assert catalog.get("EU/REGIONS").title == "Regions"
        assert catalog.get("nope") is None
        assert bool(catalog)
        assert not bool(Catalog())

    def test_datasets_are_sorted_by_id(self, catalog):
        assert [d.id for d in catalog] == sorted(d.id for d in catalog)


class TestSnapshotCompatibility:
    def test_from_snapshot_reads_the_current_format(self):
        catalog = Catalog.from_snapshot(
            {"schema_version": 2, "generated": "2026-01-01T00:00:00Z", "datasets": [RAW]}
        )
        assert len(catalog) == 1
        assert catalog.generated == "2026-01-01T00:00:00Z"

    def test_from_snapshot_tolerates_the_2020_bare_list(self):
        """v0.1 wrote a plain list with dataset_id/dataset_type keys."""
        catalog = Catalog.from_snapshot([RAW])
        assert len(catalog) == 1

    def test_malformed_records_are_skipped_not_fatal(self):
        catalog = Catalog.from_snapshot({"datasets": [RAW, {"no_id": 1}, None]})
        assert len(catalog) == 1

    def test_missing_datasets_key_gives_an_empty_catalog(self):
        assert len(Catalog.from_snapshot({})) == 0

    def test_to_snapshot_round_trip(self, catalog):
        again = Catalog.from_snapshot(catalog.to_snapshot())
        assert [d.id for d in again] == [d.id for d in catalog]

    def test_age_days(self):
        assert Catalog([], generated="1970-01-01T00:00:00Z").age_days() > 10000
        assert Catalog([]).age_days() is None
