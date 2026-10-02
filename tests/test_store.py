"""Snapshot persistence: reading, writing, and choosing between copies."""

from __future__ import annotations

import gzip
import json
import os

import pytest

from core import stac, store

DATASET = {
    "id": "A/one",
    "title": "One",
    "type": "ImageCollection",
    "provider": "A",
    "start": "2020-01-01T00:00:00Z",
}


def snapshot(**overrides) -> dict:
    base = stac.build_snapshot([dict(DATASET)])
    base.update(overrides)
    return base


class TestWriteAndRead:
    def test_gzip_round_trip(self, tmp_path):
        path = store.write_snapshot(snapshot(), str(tmp_path / "c.json.gz"))
        payload = store.read_snapshot(path)
        assert payload["count"] == 1
        assert payload["datasets"][0]["id"] == "A/one"

    def test_plain_json_round_trip(self, tmp_path):
        path = store.write_snapshot(snapshot(), str(tmp_path / "c.json"))
        assert store.read_snapshot(path)["count"] == 1

    def test_write_creates_parent_directories(self, tmp_path):
        path = store.write_snapshot(snapshot(), str(tmp_path / "a" / "b" / "c.json.gz"))
        assert os.path.exists(path)

    def test_write_leaves_no_temporary_files(self, tmp_path):
        store.write_snapshot(snapshot(), str(tmp_path / "c.json.gz"))
        assert [p.name for p in tmp_path.iterdir()] == ["c.json.gz"]

    def test_gzip_output_is_reproducible(self, tmp_path):
        """mtime=0 means the same catalog produces identical bytes."""
        payload = snapshot(generated="2026-01-01T00:00:00Z")
        first = store.write_snapshot(payload, str(tmp_path / "a.json.gz"))
        second = store.write_snapshot(payload, str(tmp_path / "b.json.gz"))
        assert open(first, "rb").read() == open(second, "rb").read()

    def test_write_is_atomic_on_failure(self, tmp_path, monkeypatch):
        target = tmp_path / "c.json.gz"
        store.write_snapshot(snapshot(), str(target))
        original = target.read_bytes()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(store.os, "replace", boom)
        with pytest.raises(OSError):
            store.write_snapshot(snapshot(generated="2099-01-01T00:00:00Z"), str(target))
        assert target.read_bytes() == original
        # and the temp file was cleaned up
        assert [p.name for p in tmp_path.iterdir()] == ["c.json.gz"]


class TestReadErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(store.SnapshotError, match="No catalog snapshot"):
            store.read_snapshot(str(tmp_path / "nope.json.gz"))

    def test_corrupt_gzip(self, tmp_path):
        path = tmp_path / "bad.json.gz"
        path.write_bytes(b"not gzip at all")
        with pytest.raises(store.SnapshotError, match="Could not read"):
            store.read_snapshot(str(path))

    def test_truncated_json(self, tmp_path):
        path = tmp_path / "bad.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write('{"datasets": [')
        with pytest.raises(store.SnapshotError):
            store.read_snapshot(str(path))

    def test_wrong_shape(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text('{"something": 1}', encoding="utf-8")
        with pytest.raises(store.SnapshotError, match="not a catalog snapshot"):
            store.read_snapshot(str(path))

    def test_newer_schema_is_refused_rather_than_mis_read(self, tmp_path):
        path = store.write_snapshot(
            snapshot(schema_version=stac.SCHEMA_VERSION + 1), str(tmp_path / "c.json.gz")
        )
        with pytest.raises(store.SnapshotError, match="newer version"):
            store.read_snapshot(path)

    def test_the_2020_bare_list_is_wrapped(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text(json.dumps([DATASET]), encoding="utf-8")
        payload = store.read_snapshot(str(path))
        assert payload["schema_version"] == 1
        assert payload["count"] == 1


class TestLoadCatalog:
    """The newer of the bundled snapshot and the user cache should win."""

    @pytest.fixture
    def paths(self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled" / store.SNAPSHOT_NAME
        cache = tmp_path / "cache" / store.SNAPSHOT_NAME
        monkeypatch.setattr(store, "bundled_path", lambda: str(bundled))
        monkeypatch.setattr(store, "cache_path", lambda: str(cache))
        return bundled, cache

    def test_fresh_cache_beats_older_bundled(self, paths):
        bundled, cache = paths
        store.write_snapshot(snapshot(generated="2020-01-01T00:00:00Z"), str(bundled))
        newer = stac.build_snapshot([dict(DATASET, id="B/two")])
        newer["generated"] = "2026-01-01T00:00:00Z"
        store.write_snapshot(newer, str(cache))
        assert [d.id for d in store.load_catalog()] == ["B/two"]

    def test_newer_bundled_beats_stale_cache_after_an_upgrade(self, paths):
        bundled, cache = paths
        fresh = stac.build_snapshot([dict(DATASET, id="NEW/one")])
        fresh["generated"] = "2026-06-01T00:00:00Z"
        store.write_snapshot(fresh, str(bundled))
        store.write_snapshot(snapshot(generated="2021-01-01T00:00:00Z"), str(cache))
        assert [d.id for d in store.load_catalog()] == ["NEW/one"]

    def test_corrupt_cache_falls_back_to_bundled(self, paths):
        bundled, cache = paths
        store.write_snapshot(snapshot(), str(bundled))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"garbage")
        assert len(store.load_catalog()) == 1

    def test_missing_bundled_uses_the_cache(self, paths):
        _, cache = paths
        store.write_snapshot(snapshot(), str(cache))
        assert len(store.load_catalog()) == 1

    def test_nothing_available_gives_an_empty_catalog_not_a_crash(self, paths):
        catalog = store.load_catalog()
        assert len(catalog) == 0

    def test_prefer_cache_false_ignores_the_cache(self, paths):
        bundled, cache = paths
        store.write_snapshot(snapshot(generated="2020-01-01T00:00:00Z"), str(bundled))
        newer = stac.build_snapshot([dict(DATASET, id="B/two")])
        newer["generated"] = "2026-01-01T00:00:00Z"
        store.write_snapshot(newer, str(cache))
        assert [d.id for d in store.load_catalog(prefer_cache=False)] == ["A/one"]


class TestStaleness:
    @pytest.fixture
    def paths(self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled" / store.SNAPSHOT_NAME
        cache = tmp_path / "cache" / store.SNAPSHOT_NAME
        monkeypatch.setattr(store, "bundled_path", lambda: str(bundled))
        monkeypatch.setattr(store, "cache_path", lambda: str(cache))
        return bundled, cache

    def test_a_fresh_snapshot_is_not_stale(self, paths):
        bundled, _ = paths
        store.write_snapshot(stac.build_snapshot([dict(DATASET)]), str(bundled))
        assert store.snapshot_age_days() == 0
        assert not store.is_stale()

    def test_an_old_snapshot_is_stale(self, paths):
        bundled, _ = paths
        store.write_snapshot(snapshot(generated="2020-01-01T00:00:00Z"), str(bundled))
        assert store.snapshot_age_days() > 365
        assert store.is_stale()

    def test_no_snapshot_counts_as_stale(self, paths):
        assert store.snapshot_age_days() is None
        assert store.is_stale()

    def test_clear_cache(self, paths):
        _, cache = paths
        assert store.clear_cache() is False
        store.write_snapshot(snapshot(), str(cache))
        assert store.clear_cache() is True
        assert not os.path.exists(str(cache))


class TestRefresh:
    def test_refresh_writes_and_returns_a_catalog(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            store.stac,
            "fetch_datasets",
            lambda **kwargs: [dict(DATASET), dict(DATASET, id="B/two")],
        )
        target = str(tmp_path / store.SNAPSHOT_NAME)
        catalog, path = store.refresh(destination=target)
        assert len(catalog) == 2
        assert path == target
        assert store.read_snapshot(path)["count"] == 2

    def test_refresh_with_no_datasets_raises_and_writes_nothing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(store.stac, "fetch_datasets", lambda **kwargs: [])
        target = str(tmp_path / store.SNAPSHOT_NAME)
        with pytest.raises(stac.StacError, match="no datasets"):
            store.refresh(destination=target)
        assert not os.path.exists(target)

    def test_a_failed_crawl_leaves_the_existing_snapshot_alone(
        self, tmp_path, monkeypatch
    ):
        target = str(tmp_path / store.SNAPSHOT_NAME)
        store.write_snapshot(snapshot(), target)
        original = open(target, "rb").read()

        def boom(**kwargs):
            raise stac.StacError("network down")

        monkeypatch.setattr(store.stac, "fetch_datasets", boom)
        with pytest.raises(stac.StacError):
            store.refresh(destination=target)
        assert open(target, "rb").read() == original


class TestBundledSnapshotShipsWithThePlugin:
    """Guards the packaging step: the plugin must work offline on first run."""

    def test_bundled_snapshot_exists_and_is_substantial(self):
        catalog = store.Catalog.from_snapshot(store.read_snapshot(store.bundled_path()))
        assert len(catalog) > 900, "bundled catalog looks truncated"

    def test_bundled_snapshot_covers_every_dataset_type(self):
        catalog = store.Catalog.from_snapshot(store.read_snapshot(store.bundled_path()))
        counts = catalog.counts_by_type()
        assert counts["ImageCollection"] > 500
        assert counts["Image"] > 50
        assert counts["FeatureCollection"] > 50

    def test_the_original_s5p_datasets_are_all_still_present(self):
        """The plugin's original purpose must not regress as coverage grows."""
        catalog = store.Catalog.from_snapshot(store.read_snapshot(store.bundled_path()))
        for product in ["AER_AI", "CH4", "CLOUD", "CO", "HCHO", "NO2", "O3", "SO2"]:
            assert f"COPERNICUS/S5P/OFFL/L3_{product}" in catalog, product
