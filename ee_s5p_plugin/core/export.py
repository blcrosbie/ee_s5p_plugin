"""Writing extracted Earth Engine data to disk.

Everything stays in text based, non-proprietary formats so the result can be
re-opened anywhere -- that was the point of the original tool and it still holds.

Two bugs from the first version are fixed here:

* CSV columns were taken from the first row only (``data[0].keys()``) and values
  written positionally, so any row with a different key set was silently written
  into the wrong columns.  :func:`write_csv` takes the union of all keys and
  writes by name.
* The GeoJSON writer buffered each sample with a square whose side was derived
  from an already-halved radius, making footprints ~30% too small.  The squares
  now come from :func:`~.geometry.regular_polygon`, whose ``sides=4`` case takes
  the pixel size directly.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Mapping, Sequence

from . import geometry

#: Extension -> human label, in the order the save dialog should offer them.
FORMATS = {
    ".geojson": "GeoJSON",
    ".csv": "Comma separated values",
    ".json": "JSON",
}

#: Candidate longitude/latitude field names, most specific first.
_LONLAT_CANDIDATES = (
    ("longitude", "latitude"),
    ("lon", "lat"),
    ("long", "lat"),
    ("x", "y"),
)


class ExportError(RuntimeError):
    """Raised when data cannot be written in the requested format."""


def field_names(rows: Sequence[Mapping]) -> list[str]:
    """Union of keys across ``rows``, preserving first-seen order."""
    names: dict[str, None] = {}
    for row in rows:
        for key in row:
            names.setdefault(str(key), None)
    return list(names)


def detect_lonlat_fields(names: Iterable[str]) -> tuple[str, str] | None:
    """Find the longitude/latitude pair in a set of field names.

    Matching is case-insensitive, and the original spelling is returned so the
    caller can index the rows with it.
    """
    lookup = {str(name).lower(): str(name) for name in names}
    for lon_key, lat_key in _LONLAT_CANDIDATES:
        if lon_key in lookup and lat_key in lookup:
            return lookup[lon_key], lookup[lat_key]
    return None


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_csv(rows: Sequence[Mapping], path: str) -> str:
    """Write a list of mappings as CSV, keyed by the union of all field names."""
    if not rows:
        raise ExportError("There is nothing to write")
    _ensure_parent(path)
    names = field_names(rows)
    # newline="" is required on Windows or csv doubles the line endings.
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in names})
    return path


def write_json(data: object, path: str, indent: int | None = 2) -> str:
    """Write any JSON-serialisable payload."""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=indent, default=str)
    return path


def rows_to_geojson(
    rows: Sequence[Mapping],
    lon_field: str | None = None,
    lat_field: str | None = None,
    resolution_m: float = 0.0,
) -> dict:
    """Turn tabular samples into a GeoJSON ``FeatureCollection``.

    With ``resolution_m`` above zero each sample becomes the square footprint of
    the pixel it was read from, which is what makes a sampled raster look right
    on the map instead of as a scatter of dots.  Otherwise samples become points.
    """
    if not rows:
        raise ExportError("There is nothing to write")

    names = field_names(rows)
    if lon_field is None or lat_field is None:
        detected = detect_lonlat_fields(names)
        if detected is None:
            raise ExportError("No longitude/latitude pair found in: " + ", ".join(names))
        lon_field, lat_field = detected

    as_polygons = resolution_m and resolution_m > 0
    features = []
    for row in rows:
        try:
            lon = float(row[lon_field])
            lat = float(row[lat_field])
        except (KeyError, TypeError, ValueError):
            # A sample with no usable position cannot be placed; skip it rather
            # than write a feature with a null geometry.
            continue

        if as_polygons:
            ring = geometry.regular_polygon(
                lon, lat, radius=resolution_m, unit="m", sides=4
            )
            geom = geometry.ring_to_geojson(ring)
        else:
            geom = {"type": "Point", "coordinates": [lon, lat]}

        properties = {}
        for name in names:
            value = row.get(name, None)
            properties[name] = None if value == "" else value
        features.append({"type": "Feature", "geometry": geom, "properties": properties})

    if not features:
        raise ExportError(
            f"None of the {len(rows)} rows had a usable "
            f"{lon_field}/{lat_field} coordinate"
        )
    return {"type": "FeatureCollection", "features": features}


def write_geojson(
    data: object,
    path: str,
    resolution_m: float = 0.0,
    lon_field: str | None = None,
    lat_field: str | None = None,
) -> str:
    """Write GeoJSON, converting tabular data on the way if needed.

    ``data`` may already be a GeoJSON mapping -- which is what a
    ``FeatureCollection`` request returns -- or a list of sample rows.
    """
    if isinstance(data, Mapping):
        if data.get("type") not in ("FeatureCollection", "Feature", "GeometryCollection"):
            raise ExportError(f"Not a GeoJSON object: type={data.get('type')!r}")
        payload = data
    else:
        payload = rows_to_geojson(
            data, lon_field=lon_field, lat_field=lat_field, resolution_m=resolution_m
        )
    return write_json(payload, path, indent=1)


def normalise_path(path: str, extension: str) -> str:
    """Make sure ``path`` ends in ``extension``, without doubling it up."""
    if not path:
        raise ExportError("No file name given")
    extension = extension if extension.startswith(".") else "." + extension
    if path.lower().endswith(extension.lower()):
        return path
    return path + extension


def save(
    data: object,
    path: str,
    extension: str | None = None,
    resolution_m: float = 0.0,
) -> str:
    """Write ``data`` in the format implied by ``extension`` (or by ``path``)."""
    if extension is None:
        extension = os.path.splitext(path)[1]
    extension = (extension or "").lower()
    if extension not in FORMATS:
        raise ExportError(
            f"Unsupported format {extension!r}; choose one of "
            f"{', '.join(sorted(FORMATS))}"
        )
    path = normalise_path(path, extension)

    if extension == ".csv":
        if isinstance(data, Mapping):
            raise ExportError(
                "Feature collections keep their nested geometry; "
                "save them as GeoJSON or JSON instead of CSV"
            )
        return write_csv(data, path)
    if extension == ".json":
        return write_json(data, path)
    return write_geojson(data, path, resolution_m=resolution_m)


def dialog_filter() -> str:
    """Qt file-dialog filter string covering :data:`FORMATS`."""
    return ";;".join(f"{label} (*{extension})" for extension, label in FORMATS.items())


def extension_from_filter(selected: str) -> str:
    """Recover the extension from whatever :func:`dialog_filter` entry was chosen."""
    for extension in FORMATS:
        if f"*{extension}" in (selected or ""):
            return extension
    return next(iter(FORMATS))
