"""Opt-in checks against the live Earth Engine STAC catalog.

These hit the network, so they are marked ``network`` and excluded from the
default run.  They are what notices Google changing the catalog's shape -- a
renamed field or a new ``gee:type`` -- before a user does.

    pytest -m network
"""

from __future__ import annotations

import pytest

from core import stac, store

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def root_catalog():
    with stac.HttpClient() as client:
        return client.get_json(stac.ROOT_CATALOG_URL)


def test_root_catalog_is_reachable_and_well_formed(root_catalog):
    assert root_catalog.get("type") == "Catalog"
    assert root_catalog.get("stac_version", "").startswith("1.")
    children = [link for link in root_catalog["links"] if link.get("rel") == "child"]
    assert len(children) > 100, f"only {len(children)} provider catalogs"


@pytest.fixture(scope="module")
def live_s5p():
    url = (
        "https://storage.googleapis.com/earthengine-stac/catalog/COPERNICUS/"
        "COPERNICUS_S5P_OFFL_L3_NO2.json"
    )
    with stac.HttpClient() as client:
        return client.get_json(url), url


def test_the_fields_the_plugin_depends_on_are_still_published(live_s5p):
    """If Google renames one of these, normalisation degrades silently."""
    record, _url = live_s5p
    for field in (
        "id",
        "title",
        "gee:type",
        "description",
        "keywords",
        "extent",
        "providers",
        "summaries",
        "license",
    ):
        assert field in record, f"'{field}' is no longer published"
    summaries = record["summaries"]
    assert "eo:bands" in summaries
    assert "gee:visualizations" in summaries
    assert record["extent"]["temporal"]["interval"]
    assert record["extent"]["spatial"]["bbox"]


def test_a_live_record_normalises_completely(live_s5p):
    record, url = live_s5p
    dataset = stac.normalise_dataset(record, stac_url=url)
    assert dataset is not None
    assert dataset["id"] == "COPERNICUS/S5P/OFFL/L3_NO2"
    assert dataset["type"] == "ImageCollection"
    assert dataset["bands"], "no bands parsed"
    assert dataset["visualizations"], "no visualisations parsed"
    assert dataset["start"] and dataset["bbox"]


def test_every_gee_type_in_the_catalog_is_accounted_for():
    """A new ``gee:type`` must be a deliberate decision, not a silent drop.

    Datasets are skipped only for types we know cannot be opened through the
    Earth Engine Python API.  Anything else appearing here means the mapping in
    :data:`stac.EE_CLASS_BY_GEE_TYPE` needs extending.
    """
    known_unsupported = {"bigquery_table"}
    skipped: dict[str, int] = {}
    datasets = stac.fetch_datasets(workers=24, skipped=skipped)

    assert len(datasets) > 900, f"only {len(datasets)} datasets crawled"
    unexpected = {
        reason: count
        for reason, count in skipped.items()
        if reason not in known_unsupported and reason != "unreachable"
    }
    assert not unexpected, (
        "unhandled gee:type values in the live catalog: "
        f"{unexpected} -- extend stac.EE_CLASS_BY_GEE_TYPE"
    )


def test_the_bundled_snapshot_is_not_badly_out_of_date():
    """Warns well before users would notice; the scheduled job keeps it current."""
    age = store.snapshot_age_days()
    assert age is not None, "the bundled snapshot has no generated timestamp"
    assert age < 180, (
        f"the bundled snapshot is {age} days old; run tools/refresh_catalog.py"
    )


def test_the_live_catalog_still_contains_the_original_s5p_products():
    datasets = {d["id"] for d in stac.fetch_datasets(workers=24)}
    for product in ("AER_AI", "CH4", "CLOUD", "CO", "HCHO", "NO2", "O3", "SO2"):
        assert f"COPERNICUS/S5P/OFFL/L3_{product}" in datasets, product
