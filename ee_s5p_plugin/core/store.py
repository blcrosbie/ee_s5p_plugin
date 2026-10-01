"""Where the catalog snapshot lives on disk, and how it stays fresh.

Two locations are in play:

``<plugin>/data/gee_catalog.json.gz``
    The snapshot shipped inside the plugin.  Read-only -- a plugin directory is
    not reliably writable, and an upgrade replaces it anyway.

``<profile>/ee_s5p_plugin/gee_catalog.json.gz``
    The refreshed copy, written next to the user's QGIS settings.  Preferred
    whenever it is newer than the bundled one.

So a fresh install works offline from the bundled snapshot, a refresh never needs
elevated permissions, and a plugin upgrade that ships a newer snapshot wins over
a stale cache automatically.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import shutil
import tempfile

from . import dates, stac
from .catalog import Catalog

LOGGER = logging.getLogger(__name__)

SNAPSHOT_NAME = "gee_catalog.json.gz"

#: Offer a refresh once the snapshot is older than this.
STALE_AFTER_DAYS = 30


class SnapshotError(RuntimeError):
    """Raised when a snapshot file is missing or unusable."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def plugin_dir() -> str:
    """Directory of the installed plugin package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundled_path() -> str:
    return os.path.join(plugin_dir(), "data", SNAPSHOT_NAME)


def cache_dir() -> str:
    """Writable directory for the refreshed snapshot.

    Uses the active QGIS profile when running inside QGIS so each profile keeps
    its own copy, and falls back to the platform temp directory otherwise (which
    is what the tests and the refresh CLI get).
    """
    try:
        from qgis.core import QgsApplication
    except ImportError:
        base = os.path.join(tempfile.gettempdir(), "ee_s5p_plugin")
    else:
        settings_path = QgsApplication.qgisSettingsDirPath() or tempfile.gettempdir()
        base = os.path.join(settings_path, "ee_s5p_plugin")
    return base


def cache_path() -> str:
    return os.path.join(cache_dir(), SNAPSHOT_NAME)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _open_snapshot(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def read_snapshot(path: str) -> dict:
    """Load and validate a snapshot file."""
    if not os.path.exists(path):
        raise SnapshotError(f"No catalog snapshot at {path}")
    try:
        with _open_snapshot(path) as handle:
            payload = json.load(handle)
    except (OSError, ValueError, EOFError) as error:
        raise SnapshotError(f"Could not read {path}: {error}") from error

    if isinstance(payload, list):
        # The 2020 format was a bare list; wrap it so callers see one shape.
        return {"schema_version": 1, "datasets": payload, "count": len(payload)}
    if not isinstance(payload, dict) or "datasets" not in payload:
        raise SnapshotError(f"{path} is not a catalog snapshot")

    version = payload.get("schema_version")
    if version is not None and version > stac.SCHEMA_VERSION:
        raise SnapshotError(
            f"{path} was written by a newer version of the plugin "
            f"(schema {version} > {stac.SCHEMA_VERSION})"
        )
    return payload


def write_snapshot(snapshot: dict, path: str) -> str:
    """Write a snapshot atomically, creating parent directories as needed."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # Opened outside a `with` deliberately: the name is needed for os.replace.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="wb", dir=directory, prefix=".catalog-", suffix=".tmp", delete=False
    )
    temp_path = handle.name
    try:
        with handle:
            payload = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
            if path.endswith(".gz"):
                # mtime=0 and an empty filename keep the bytes reproducible for
                # the same input: otherwise gzip stamps the time and the random
                # temp file name into the header.
                with gzip.GzipFile(
                    filename="", fileobj=handle, mode="wb", compresslevel=9, mtime=0
                ) as zipped:
                    zipped.write(payload)
            else:
                handle.write(json.dumps(snapshot, indent=1).encode("utf-8"))
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise
    return path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _generated(snapshot: dict) -> object:
    return dates.parse(snapshot.get("generated"))


def load_catalog(prefer_cache: bool = True) -> Catalog:
    """Load the best available snapshot as a :class:`~.catalog.Catalog`.

    Prefers the refreshed cache when it is newer than the bundled snapshot, and
    falls back cleanly in either direction so a corrupted cache cannot stop the
    plugin from opening.
    """
    candidates: list[dict] = []
    for path, label in ((cache_path(), "cache"), (bundled_path(), "bundled")):
        if prefer_cache is False and label == "cache":
            continue
        try:
            snapshot = read_snapshot(path)
        except SnapshotError as error:
            LOGGER.debug("%s snapshot unusable: %s", label, error)
            continue
        snapshot["_path"] = path
        snapshot["_origin"] = label
        candidates.append(snapshot)

    if not candidates:
        LOGGER.warning("No catalog snapshot available; starting empty")
        return Catalog()

    def sort_key(snapshot: dict):
        generated = _generated(snapshot)
        # Undated snapshots sort oldest; the bundled copy breaks ties.
        return (
            generated is not None,
            generated or dates.parse("1970-01-01"),
            snapshot["_origin"] == "cache",
        )

    best = max(candidates, key=sort_key)
    LOGGER.info(
        "Loaded %s catalog snapshot (%s datasets, generated %s)",
        best["_origin"],
        best.get("count", len(best.get("datasets", ()))),
        best.get("generated", "unknown"),
    )
    return Catalog.from_snapshot(best)


def snapshot_age_days() -> int | None:
    """Age in days of whichever snapshot :func:`load_catalog` would pick."""
    ages = []
    for path in (cache_path(), bundled_path()):
        try:
            snapshot = read_snapshot(path)
        except SnapshotError:
            continue
        age = dates.day_span(snapshot.get("generated"), dates.now())
        if age is not None:
            ages.append(age)
    return min(ages) if ages else None


def is_stale(threshold_days: int = STALE_AFTER_DAYS) -> bool:
    age = snapshot_age_days()
    return age is None or age > threshold_days


def refresh(
    progress: stac.ProgressCallback | None = None,
    workers: int = stac.DEFAULT_WORKERS,
    destination: str | None = None,
    proxies: dict | None = None,
) -> tuple[Catalog, str]:
    """Crawl the live STAC catalog and save it as the user's cache.

    Returns the new catalog and the path written.  Raises
    :class:`~.stac.StacError` if the catalog cannot be reached, leaving any
    existing snapshot untouched.
    """
    skipped: dict[str, int] = {}
    datasets = stac.fetch_datasets(
        workers=workers, progress=progress, proxies=proxies, skipped=skipped
    )
    if not datasets:
        raise stac.StacError("The Earth Engine catalog returned no datasets")
    if skipped:
        LOGGER.info("Refresh skipped records: %s", skipped)

    snapshot = stac.build_snapshot(datasets)
    path = write_snapshot(snapshot, destination or cache_path())
    return Catalog.from_snapshot(snapshot), path


def clear_cache() -> bool:
    """Delete the refreshed snapshot, falling back to the bundled one."""
    path = cache_path()
    if not os.path.exists(path):
        return False
    try:
        os.unlink(path)
    except OSError as error:
        LOGGER.warning("Could not remove %s: %s", path, error)
        return False
    return True


def install_as_bundled(snapshot_path: str) -> str:
    """Copy a refreshed snapshot over the bundled one (used by the build tools)."""
    target = bundled_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(snapshot_path, target)
    return target
