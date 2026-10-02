"""The in-memory catalog: a dataset record type plus search and filtering.

The first version of the plugin kept the catalog as a list of raw dicts and
re-derived the type/publisher/tag indexes on every keystroke, from inside the
dock widget.  With 92 hand-picked datasets that was survivable; with the full
1100+ dataset catalog it is not.  This module owns that logic instead, builds its
indexes once, and has no Qt or Earth Engine dependency so it can be tested.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import dates, geometry, stac

#: Dataset types the plugin can issue requests for.
DATASET_TYPES = ("ImageCollection", "Image", "FeatureCollection")


@dataclass(frozen=True)
class Band:
    """One band of an image or image collection."""

    name: str
    description: str | None = None
    units: str | None = None
    gsd: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> Band:
        return cls(
            name=raw.get("name", ""),
            description=raw.get("description"),
            units=raw.get("units"),
            gsd=_as_float(raw.get("gsd")),
            minimum=_as_float(raw.get("minimum")),
            maximum=_as_float(raw.get("maximum")),
        )

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.units:
            parts.append(f"({self.units})")
        return " ".join(parts)


@dataclass(frozen=True)
class Visualisation:
    """A default rendering Google suggests for a dataset."""

    name: str
    kind: str = "image"
    bands: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    gamma: float | None = None
    palette: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> Visualisation:
        return cls(
            name=raw.get("name") or "Default",
            kind=raw.get("kind") or "image",
            bands=tuple(raw.get("bands") or ()),
            min=_as_float(raw.get("min")),
            max=_as_float(raw.get("max")),
            gamma=_as_float(raw.get("gamma")),
            palette=tuple(raw.get("palette") or ()),
        )

    @property
    def label(self) -> str:
        if self.bands:
            return f"{self.name} – {', '.join(self.bands)}"
        return self.name


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Dataset:
    """One entry in the Earth Engine Data Catalog."""

    id: str
    title: str
    type: str
    provider: str = ""
    publisher: str | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    start: str | None = None
    end: str | None = None
    ongoing: bool = False
    bbox: tuple[float, float, float, float] | None = None
    bands: tuple[Band, ...] = ()
    visualizations: tuple[Visualisation, ...] = ()
    gsd: float | None = None
    interval: dict | None = None
    license: str | None = None
    terms_of_use: str | None = None
    citation: str | None = None
    doi: str | None = None
    version: str | None = None
    deprecated: bool = False
    status: str | None = None
    doc_url: str | None = None
    stac_url: str | None = None
    preview_url: str | None = None
    #: Lower-cased haystack built once, for substring search.
    _haystack: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: dict) -> Dataset:
        bbox = raw.get("bbox")
        dataset = cls(
            id=raw["id"],
            title=raw.get("title") or raw["id"],
            type=raw.get("type") or "ImageCollection",
            provider=raw.get("provider") or raw["id"].split("/")[0],
            publisher=raw.get("publisher"),
            description=raw.get("description") or "",
            tags=tuple(raw.get("tags") or ()),
            categories=tuple(raw.get("categories") or ()),
            start=raw.get("start"),
            end=raw.get("end"),
            ongoing=bool(raw.get("ongoing")),
            bbox=tuple(float(v) for v in bbox[:4]) if bbox and len(bbox) >= 4 else None,
            bands=tuple(Band.from_dict(b) for b in raw.get("bands") or ()),
            visualizations=tuple(
                Visualisation.from_dict(v) for v in raw.get("visualizations") or ()
            ),
            gsd=_as_float(raw.get("gsd")),
            interval=raw.get("interval"),
            license=raw.get("license"),
            terms_of_use=raw.get("terms_of_use"),
            citation=raw.get("citation"),
            doi=raw.get("doi"),
            version=raw.get("version"),
            deprecated=bool(raw.get("deprecated")),
            status=raw.get("status"),
            doc_url=raw.get("doc_url"),
            stac_url=raw.get("stac_url"),
            preview_url=raw.get("preview_url"),
        )
        dataset._haystack = " ".join(
            (
                dataset.id,
                dataset.title,
                dataset.publisher or "",
                " ".join(dataset.tags),
                " ".join(dataset.categories),
                " ".join(band.name for band in dataset.bands),
                stac.plain_text(dataset.description, limit=600),
            )
        ).lower()
        return dataset

    # -- derived views ----------------------------------------------------

    @property
    def band_names(self) -> tuple[str, ...]:
        return tuple(band.name for band in self.bands)

    @property
    def availability(self) -> str:
        end = None if self.ongoing else self.end
        return dates.describe_range(self.start, end)

    @property
    def effective_end(self) -> str | None:
        """``None`` (meaning "up to today") for collections still being added to."""
        return None if self.ongoing else self.end

    @property
    def cadence(self) -> str | None:
        """``gee:interval`` rendered as e.g. ``every 8 days``."""
        if not isinstance(self.interval, dict):
            return None
        every = self.interval.get("interval")
        unit = self.interval.get("unit")
        if not every or not unit:
            return None
        plural = unit if every == 1 else f"{unit}s"
        kind = self.interval.get("type")
        prefix = "revisit every" if kind == "revisit_interval" else "every"
        return f"{prefix} {every} {plural}"

    @property
    def is_global(self) -> bool:
        if not self.bbox:
            return False
        west, south, east, north = self.bbox
        return west <= -179 and south <= -89 and east >= 179 and north >= 89

    def band(self, name: str) -> Band | None:
        for band in self.bands:
            if band.name == name:
                return band
        return None

    def visualisation_for(self, band_name: str) -> Visualisation | None:
        """The suggested rendering that covers ``band_name``, if any."""
        for viz in self.visualizations:
            if band_name in viz.bands:
                return viz
        return None

    def default_resolution(self) -> float | None:
        """Native ground resolution in metres, if Google publishes one."""
        if self.gsd:
            return self.gsd
        for band in self.bands:
            if band.gsd:
                return band.gsd
        return None

    def matches_text(self, query: str) -> bool:
        """Case-insensitive AND over whitespace-separated terms."""
        if not query:
            return True
        return all(term in self._haystack for term in query.lower().split())

    def to_dict(self) -> dict:
        """Round-trip back to the snapshot representation."""
        raw = {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "provider": self.provider,
            "publisher": self.publisher,
            "description": self.description,
            "tags": list(self.tags),
            "categories": list(self.categories),
            "start": self.start,
            "end": self.end,
            "ongoing": self.ongoing or None,
            "bbox": list(self.bbox) if self.bbox else None,
            "gsd": self.gsd,
            "interval": self.interval,
            "license": self.license,
            "terms_of_use": self.terms_of_use,
            "citation": self.citation,
            "doi": self.doi,
            "version": self.version,
            "deprecated": self.deprecated or None,
            "status": self.status,
            "doc_url": self.doc_url,
            "stac_url": self.stac_url,
            "preview_url": self.preview_url,
        }
        return {k: v for k, v in raw.items() if v not in (None, "", [], {})}


@dataclass
class Filters:
    """Everything the user can narrow the catalog by.

    Defaults are deliberately permissive except for ``include_deprecated``: a
    quarter of the catalog is deprecated, and offering those first would be
    actively misleading.
    """

    text: str = ""
    types: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    tags_match_all: bool = False
    start: str | None = None
    end: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    include_deprecated: bool = False
    bands_only: bool = False

    def is_empty(self) -> bool:
        return not any(
            (
                self.text,
                self.types,
                self.providers,
                self.tags,
                self.start,
                self.end,
                self.bbox,
                self.include_deprecated,
                self.bands_only,
            )
        )

    def accepts(self, dataset: Dataset) -> bool:
        if dataset.deprecated and not self.include_deprecated:
            return False
        if self.types and dataset.type not in self.types:
            return False
        if self.providers and dataset.provider not in self.providers:
            return False
        if self.bands_only and not dataset.bands:
            return False
        if self.tags:
            present = set(dataset.tags)
            wanted = set(self.tags)
            if self.tags_match_all:
                if not wanted.issubset(present):
                    return False
            elif present.isdisjoint(wanted):
                return False
        if (self.start or self.end) and not dates.overlaps(
            dataset.start, dataset.effective_end, self.start, self.end
        ):
            return False
        if (
            self.bbox
            and dataset.bbox
            and not geometry.bboxes_intersect(list(self.bbox), list(dataset.bbox))
        ):
            return False
        return dataset.matches_text(self.text)


class Catalog:
    """A searchable collection of :class:`Dataset` records."""

    def __init__(
        self,
        datasets: Iterable[Dataset] = (),
        generated: str | None = None,
        source: str | None = None,
    ):
        self._datasets: list[Dataset] = sorted(datasets, key=lambda d: d.id)
        self._by_id = {dataset.id: dataset for dataset in self._datasets}
        self.generated = generated
        self.source = source

    # -- construction -----------------------------------------------------

    @classmethod
    def from_snapshot(cls, snapshot: dict | list) -> Catalog:
        """Build from the payload :mod:`.store` loads off disk.

        Tolerates the 2020 format, which was a bare list of dicts, and skips
        individual malformed records rather than refusing to open at all -- a
        truncated cache should cost the user some datasets, not the whole plugin.
        """
        if isinstance(snapshot, dict):
            raw_datasets = snapshot.get("datasets") or []
            generated = snapshot.get("generated")
            source = snapshot.get("source")
        elif isinstance(snapshot, list):
            raw_datasets, generated, source = snapshot, None, None
        else:
            raw_datasets, generated, source = [], None, None

        datasets = []
        for raw in raw_datasets:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            try:
                datasets.append(Dataset.from_dict(raw))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return cls(datasets, generated=generated, source=source)

    def to_snapshot(self) -> dict:
        return stac.build_snapshot(
            [d.to_dict() for d in self._datasets],
            source=self.source or stac.ROOT_CATALOG_URL,
        )

    # -- container protocol ----------------------------------------------

    def __len__(self) -> int:
        return len(self._datasets)

    def __iter__(self) -> Iterator[Dataset]:
        return iter(self._datasets)

    def __contains__(self, dataset_id: object) -> bool:
        return dataset_id in self._by_id

    def __bool__(self) -> bool:
        return bool(self._datasets)

    def get(self, dataset_id: str) -> Dataset | None:
        return self._by_id.get(dataset_id)

    @property
    def datasets(self) -> list[Dataset]:
        return list(self._datasets)

    # -- indexes ----------------------------------------------------------

    def types(self) -> list[str]:
        """Present dataset types, in the plugin's preferred display order."""
        present = {dataset.type for dataset in self._datasets}
        ordered = [t for t in DATASET_TYPES if t in present]
        return ordered + sorted(present - set(ordered))

    def providers(self) -> list[str]:
        return sorted(
            {dataset.provider for dataset in self._datasets if dataset.provider}
        )

    def publishers(self) -> list[str]:
        return sorted({d.publisher for d in self._datasets if d.publisher})

    def tags(self, within: Sequence[Dataset] | None = None) -> list[str]:
        """All tags, optionally restricted to a subset of datasets.

        Passing the current result set is what makes tag completion useful: it
        only suggests tags that can still narrow the selection further.
        """
        pool = self._datasets if within is None else within
        return sorted({tag for dataset in pool for tag in dataset.tags})

    def tag_counts(self, within: Sequence[Dataset] | None = None) -> dict[str, int]:
        pool = self._datasets if within is None else within
        counts: dict[str, int] = {}
        for dataset in pool:
            for tag in dataset.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for dataset in self._datasets:
            counts[dataset.type] = counts.get(dataset.type, 0) + 1
        return counts

    @property
    def deprecated_count(self) -> int:
        return sum(1 for dataset in self._datasets if dataset.deprecated)

    # -- search -----------------------------------------------------------

    def search(self, filters: Filters | None = None) -> list[Dataset]:
        if filters is None:
            filters = Filters()
        return [dataset for dataset in self._datasets if filters.accepts(dataset)]

    def age_days(self) -> int | None:
        """How stale the snapshot is, or ``None`` if it carries no timestamp."""
        return dates.day_span(self.generated, dates.now())
