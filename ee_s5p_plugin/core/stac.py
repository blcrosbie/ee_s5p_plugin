"""Access to the Google Earth Engine STAC catalog.

Google publishes machine readable `STAC <https://stacspec.org>`_ metadata for
every dataset in the Earth Engine Data Catalog.  Crawling that replaces the HTML
scraping the first version of this plugin relied on: the STAC records are
stable, carry the band / visualisation / extent metadata the plugin previously
had to guess at, and cover the *whole* catalog rather than a hand-picked subset.

The crawl is two levels deep::

    catalog.json                 -> one child catalog per provider (AAFC, NASA, ...)
      <PROVIDER>/catalog.json    -> one child link per dataset
        <DATASET>.json           -> the collection record we normalise

Nothing in this module imports Qt, QGIS or ``ee``, so it is exercised directly
by the unit tests and by ``tools/refresh_catalog.py``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

ROOT_CATALOG_URL = "https://earthengine-stac.storage.googleapis.com/catalog/catalog.json"

#: Bumped whenever :func:`normalise_dataset` changes shape, so that a cache
#: written by an older plugin version is discarded instead of mis-read.
SCHEMA_VERSION = 2

DEFAULT_WORKERS = 16
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3

#: ``gee:type`` as published by Google -> the ``ee`` class that can open it.
#: ``bigquery_table`` datasets are deliberately absent: they live in BigQuery and
#: cannot be opened through the Earth Engine Python API at all, so listing them
#: would only offer the user requests that are guaranteed to fail.  The refresh
#: CLI reports how many records were skipped for this reason.
EE_CLASS_BY_GEE_TYPE = {
    "image": "Image",
    "image_collection": "ImageCollection",
    "table": "FeatureCollection",
    "table_collection": "FeatureCollection",
}

#: A dataset whose newest data is this recent -- relative to when the snapshot
#: was taken -- is assumed to still be growing.  Google publishes the timestamp
#: of the *latest ingested* item as the end of the temporal extent, so a literal
#: reading of it would hide today's imagery behind a week-old snapshot.
ONGOING_TOLERANCE_DAYS = 45

DOC_URL_TEMPLATE = "https://developers.google.com/earth-engine/datasets/catalog/{slug}"


class StacError(RuntimeError):
    """Raised when the catalog cannot be read."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class HttpClient:
    """Minimal JSON fetcher.

    Uses ``requests`` when it is importable -- it ships with QGIS on every
    platform and gives us connection reuse across the ~1200 requests a full
    crawl makes -- and falls back to :mod:`urllib` so the module stays usable in
    a bare Python environment.
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        proxies: dict | None = None,
    ):
        self.timeout = timeout
        self.retries = max(1, retries)
        self._session = None
        try:
            import requests
        except ImportError:
            LOGGER.debug("requests unavailable, falling back to urllib")
        else:
            self._session = requests.Session()
            self._session.headers["User-Agent"] = "ee_s5p_plugin (QGIS plugin)"
            if proxies:
                self._session.proxies.update(proxies)

    def get_json(self, url: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._get_json_once(url)
            except Exception as error:
                last_error = error
                LOGGER.debug(
                    "GET %s failed (attempt %s/%s): %s",
                    url,
                    attempt + 1,
                    self.retries,
                    error,
                )
        raise StacError(f"Could not fetch {url}: {last_error}") from last_error

    def _get_json_once(self, url: str) -> dict:
        if self._session is not None:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        import urllib.request

        request = urllib.request.Request(
            url, headers={"User-Agent": "ee_s5p_plugin (QGIS plugin)"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as handle:
            return json.load(handle)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _child_links(node: dict) -> list[str]:
    return [
        link["href"]
        for link in node.get("links") or ()
        if link.get("rel") == "child" and link.get("href")
    ]


def _link_href(node: dict, rel: str) -> str | None:
    for link in node.get("links") or ():
        if link.get("rel") == rel and link.get("href"):
            return link["href"]
    return None


def dataset_slug(dataset_id: str) -> str:
    """``COPERNICUS/S5P/OFFL/L3_NO2`` -> ``COPERNICUS_S5P_OFFL_L3_NO2``."""
    return dataset_id.replace("/", "_")


def _scalar(value: Any) -> Any:
    """STAC visualisations wrap single values in lists; unwrap them."""
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return value[0]
    return value


def _temporal_extent(record: dict) -> tuple[str | None, str | None]:
    intervals = ((record.get("extent") or {}).get("temporal") or {}).get("interval") or []
    starts = [i[0] for i in intervals if i and i[0]]
    ends = [i[1] for i in intervals if len(i) > 1 and i[1]]
    start = min(starts) if starts else None
    # An open ended interval is published as ``null``, meaning "still growing";
    # only report an end date when every interval has one.
    end = max(ends) if ends and len(ends) == len(intervals) else None
    return start, end


def _spatial_extent(record: dict) -> list[float] | None:
    boxes = ((record.get("extent") or {}).get("spatial") or {}).get("bbox") or []
    boxes = [b for b in boxes if b and len(b) >= 4]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _bands(record: dict) -> list[dict]:
    summaries = record.get("summaries") or {}
    bands = []
    for band in summaries.get("eo:bands") or ():
        if not isinstance(band, dict) or not band.get("name"):
            continue
        name = band["name"]
        entry = {
            "name": name,
            "description": (band.get("description") or "").strip() or None,
            "units": band.get("gee:units"),
            "gsd": _scalar(band.get("gsd")),
            "wavelength": band.get("center_wavelength"),
            "offset": band.get("gee:offset"),
            "scale": band.get("gee:scale"),
        }
        # Per band value ranges live beside ``eo:bands``, keyed by band name.
        stats = summaries.get(name)
        if isinstance(stats, dict):
            entry["minimum"] = stats.get("minimum")
            entry["maximum"] = stats.get("maximum")
        bands.append({k: v for k, v in entry.items() if v is not None})
    return bands


def _visualisations(record: dict) -> list[dict]:
    out = []
    summaries = record.get("summaries") or {}
    for viz in summaries.get("gee:visualizations") or ():
        if not isinstance(viz, dict):
            continue
        name = viz.get("display_name") or "Default"
        image_vis = (viz.get("image_visualization") or {}).get("band_vis")
        table_vis = viz.get("table_visualization")
        if image_vis:
            entry = {
                "name": name,
                "kind": "image",
                "bands": list(image_vis.get("bands") or ()),
                "min": _scalar(image_vis.get("min")),
                "max": _scalar(image_vis.get("max")),
                "gamma": _scalar(image_vis.get("gamma")),
                "palette": list(image_vis.get("palette") or ()),
            }
        elif table_vis:
            entry = {
                "name": name,
                "kind": "table",
                "color": table_vis.get("color"),
                "fill_color": table_vis.get("fill_color"),
                "point_size": table_vis.get("point_size"),
                "width": table_vis.get("width"),
            }
        else:
            continue
        out.append({k: v for k, v in entry.items() if v not in (None, [], ())})
    return out


def _publisher(record: dict) -> str | None:
    """The organisation that produced the data, not the Earth Engine host."""
    providers = record.get("providers") or ()
    for wanted in ("producer", "licensor", "processor"):
        for provider in providers:
            if wanted in (provider.get("roles") or ()) and provider.get("name"):
                return provider["name"]
    for provider in providers:
        # Anything other than Google's own "host" entry is more informative.
        if "host" not in (provider.get("roles") or ()) and provider.get("name"):
            return provider["name"]
    return None


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def plain_text(markdown: str | None, limit: int | None = None) -> str:
    """Flatten the markdown/HTML descriptions into something a tooltip can use."""
    if not markdown:
        return ""
    text = _MD_LINK.sub(r"\1", markdown)
    text = _HTML_TAG.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def normalise_dataset(record: dict, stac_url: str | None = None) -> dict | None:
    """Reduce a STAC collection record to the fields the plugin uses.

    Returns ``None`` for records that are not datasets we can open: catalogs,
    records with no id, or an unknown ``gee:type``.
    """
    dataset_id = record.get("id")
    if not dataset_id or record.get("type") != "Collection":
        return None

    gee_type = record.get("gee:type")
    ee_class = EE_CLASS_BY_GEE_TYPE.get(gee_type)
    if ee_class is None:
        LOGGER.debug("Skipping %s: unsupported gee:type %r", dataset_id, gee_type)
        return None

    start, end = _temporal_extent(record)
    summaries = record.get("summaries") or {}
    status = record.get("gee:status")

    normalised = {
        "id": dataset_id,
        "title": (record.get("title") or dataset_id).strip(),
        "type": ee_class,
        "gee_type": gee_type,
        "provider": dataset_id.split("/")[0],
        "publisher": _publisher(record),
        "description": record.get("description") or "",
        "tags": sorted({str(k).lower() for k in record.get("keywords") or ()}),
        "categories": list(record.get("gee:categories") or ()),
        "start": start,
        "end": end,
        "bbox": _spatial_extent(record),
        "bands": _bands(record),
        "visualizations": _visualisations(record),
        "gsd": _scalar(summaries.get("gsd")),
        "interval": record.get("gee:interval"),
        "license": record.get("license"),
        "terms_of_use": record.get("gee:terms_of_use"),
        "citation": record.get("sci:citation"),
        "doi": record.get("sci:doi"),
        "version": record.get("version"),
        "deprecated": bool(record.get("deprecated")) or status == "deprecated",
        "status": status,
        "doc_url": DOC_URL_TEMPLATE.format(slug=dataset_slug(dataset_id)),
        "stac_url": stac_url or _link_href(record, "self"),
        "preview_url": _link_href(record, "preview"),
    }
    # Drop empties so the shipped snapshot stays small.  ``False`` counts as
    # empty: every boolean field here defaults to false when absent, and writing
    # ``"deprecated": false`` for a thousand datasets is pure overhead.
    return {k: v for k, v in normalised.items() if not _is_empty(v)}


def _is_empty(value: Any) -> bool:
    return value is None or value is False or value in ("", [], {})


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

#: Called as ``progress(done, total, message)``; return ``False`` to cancel.
ProgressCallback = Callable[[int, int, str], bool]


def _report(
    progress: ProgressCallback | None, done: int, total: int, message: str
) -> bool:
    if progress is None:
        return True
    return progress(done, total, message) is not False


def _safe_child_links(client: HttpClient) -> Callable[[str], list[str]]:
    def fetch(url: str) -> list[str]:
        try:
            return _child_links(client.get_json(url))
        except Exception as error:
            # One unreachable provider catalog must not abort the whole crawl.
            LOGGER.warning("Skipping provider catalog %s: %s", url, error)
            return []

    return fetch


def _safe_dataset(
    client: HttpClient, skipped: dict | None = None
) -> Callable[[str], dict | None]:
    def note(reason: str) -> None:
        if skipped is not None:
            skipped[reason] = skipped.get(reason, 0) + 1

    def fetch(url: str) -> dict | None:
        try:
            record = client.get_json(url)
        except Exception as error:
            LOGGER.warning("Skipping dataset %s: %s", url, error)
            note("unreachable")
            return None
        dataset = normalise_dataset(record, stac_url=url)
        if dataset is None:
            note(str(record.get("gee:type") or "unknown"))
        return dataset

    return fetch


def dataset_urls(
    root_url: str = ROOT_CATALOG_URL,
    client: HttpClient | None = None,
    workers: int = DEFAULT_WORKERS,
    progress: ProgressCallback | None = None,
) -> list[str]:
    """Collect every dataset record URL reachable from ``root_url``."""
    owned = client is None
    client = client or HttpClient()
    try:
        root = client.get_json(root_url)
        provider_urls = _child_links(root)
        if not provider_urls:
            raise StacError(f"No provider catalogs found at {root_url}")

        urls: list[str] = []
        total = len(provider_urls)
        if not _report(progress, 0, total, "Reading provider catalogs"):
            return []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for done, children in enumerate(
                pool.map(_safe_child_links(client), provider_urls), start=1
            ):
                urls.extend(children)
                if not _report(
                    progress, done, total, f"Read {done}/{total} provider catalogs"
                ):
                    return urls
        # Providers occasionally cross-link, so de-duplicate while keeping order.
        return list(dict.fromkeys(urls))
    finally:
        if owned:
            client.close()


def fetch_datasets(
    root_url: str = ROOT_CATALOG_URL,
    workers: int = DEFAULT_WORKERS,
    progress: ProgressCallback | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    proxies: dict | None = None,
    skipped: dict | None = None,
) -> list[dict]:
    """Crawl the whole catalog and return normalised dataset records.

    Pass a dict as ``skipped`` to be told why records were left out, keyed by
    ``gee:type`` (or ``"unreachable"``).  See :data:`EE_CLASS_BY_GEE_TYPE`.
    """
    with HttpClient(timeout=timeout, proxies=proxies) as client:
        urls = dataset_urls(root_url, client=client, workers=workers, progress=progress)
        if not urls:
            return []

        datasets: list[dict] = []
        total = len(urls)
        if not _report(progress, 0, total, f"Reading {total} datasets"):
            return []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for done, dataset in enumerate(
                pool.map(_safe_dataset(client, skipped), urls), start=1
            ):
                if dataset is not None:
                    datasets.append(dataset)
                if (done % 25 == 0 or done == total) and not _report(
                    progress, done, total, f"Read {done}/{total} datasets"
                ):
                    break
        datasets.sort(key=lambda d: d["id"])
        return datasets


def mark_ongoing(
    datasets: Iterable[dict],
    reference: datetime | None = None,
    tolerance_days: int = ONGOING_TOLERANCE_DAYS,
) -> int:
    """Flag datasets that are still being added to, in place.

    Google publishes the timestamp of the newest *ingested* item as the end of a
    collection's temporal extent, so an actively updated collection looks closed
    as soon as the snapshot ages.  Anything not deprecated whose newest item is
    within ``tolerance_days`` of the snapshot is marked ``ongoing``; the time
    filter then treats its end date as "today" rather than as a hard boundary.
    """
    from . import dates as _dates

    reference = reference or datetime.now(timezone.utc)
    marked = 0
    for dataset in datasets:
        end = _dates.parse(dataset.get("end"))
        ongoing = end is None or (
            not dataset.get("deprecated") and (reference - end).days <= tolerance_days
        )
        if ongoing:
            dataset["ongoing"] = True
            marked += 1
        else:
            dataset.pop("ongoing", None)
    return marked


def build_snapshot(datasets: Sequence[dict], source: str = ROOT_CATALOG_URL) -> dict:
    """Wrap ``datasets`` with the header :mod:`.store` validates on load."""
    generated = datetime.now(timezone.utc)
    datasets = list(datasets)
    ongoing = mark_ongoing(datasets, reference=generated)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "count": len(datasets),
        "ongoing": ongoing,
        "datasets": datasets,
    }


def summarise(datasets: Iterable[dict]) -> dict[str, int]:
    """Counts by dataset type -- used by the refresh CLI and the tests."""
    counts: dict[str, int] = {}
    for dataset in datasets:
        key = dataset.get("type", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
