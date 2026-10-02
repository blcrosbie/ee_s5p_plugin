"""The only module that talks to the ``ee`` Python API.

``ee`` is imported lazily, for two reasons: the plugin must be able to open and
browse the catalog with no Earth Engine credentials at all, and ``ee`` arrives via
the separate "Google Earth Engine" QGIS plugin, which may be installed after this
one.  Keeping the import behind :func:`require_ee` means a missing dependency
produces one clear message instead of an import-time crash.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from . import dates, geometry
from .request import ExtractRequest

LOGGER = logging.getLogger(__name__)

#: Where users get the dependency, quoted in the error message.
EE_PLUGIN_NAME = "Google Earth Engine"
EE_PLUGIN_URL = "https://gee-community.github.io/qgis-earthengine-plugin/"


class EarthEngineUnavailable(RuntimeError):
    """``ee`` is not importable, or is not initialised."""


class EarthEngineError(RuntimeError):
    """A request reached Earth Engine and was rejected."""


class EarthEngineNotInitialised(EarthEngineUnavailable):
    """``ee`` imported, but ``ee.Initialize`` never succeeded.

    Its own subclass because the remedy is completely different from a missing
    install: the dependency is present, it just has no usable credentials.
    """


#: What to tell the user when Earth Engine was never initialised.  The Earth
#: Engine plugin cannot offer its own sign-in command in this state, because it
#: fails inside ``classFactory`` and so never finishes loading.
NOT_INITIALISED_ADVICE = """\
Earth Engine has not been signed in to in this QGIS session.

The 'Google Earth Engine' plugin signs in at start-up. If that failed it never \
finished loading, so its own sign-in command is unavailable too.

Sign in from the QGIS Python Console (Plugins > Python Console):

    import ee
    ee.Authenticate()
    ee.Initialize(project='your-cloud-project')

A browser will open. If nothing happens, use
ee.Authenticate(auth_mode='notebook'), which prints a URL to paste instead.

Then restart QGIS so the Earth Engine plugin loads cleanly."""


_ee = None


def require_ee():
    """Import and return the ``ee`` module, or explain why we cannot."""
    global _ee
    if _ee is not None:
        return _ee
    try:
        import ee
    except ImportError as error:
        raise EarthEngineUnavailable(
            "The Earth Engine Python API is not available.\n\n"
            f"Install the '{EE_PLUGIN_NAME}' QGIS plugin, which provides it, "
            f"then restart QGIS.\n{EE_PLUGIN_URL}"
        ) from error
    _ee = ee
    return _ee


def is_available() -> bool:
    try:
        require_ee()
    except EarthEngineUnavailable:
        return False
    return True


def is_initialised() -> bool:
    """Has ``ee.Initialize`` already succeeded in this session?

    The "Google Earth Engine" plugin normally does this at QGIS start-up, so in
    practice this asks whether it got that far.  Importing ``ee`` is *not* enough:
    the module imports fine from that plugin's bundled copy even when its
    ``classFactory`` blew up before initialising, and then the first ``ee`` call
    raises "Earth Engine client library not initialized".

    Uses ``ee.data.is_initialized()`` where it exists (earthengine-api 1.x), which
    costs nothing, and falls back to a cheap server round trip on older versions.
    """
    try:
        ee = require_ee()
    except EarthEngineUnavailable:
        return False

    probe = getattr(ee.data, "is_initialized", None)
    if probe is not None:
        try:
            return bool(probe() if callable(probe) else probe)
        except Exception:
            return False
    try:
        # Older ee: no predicate, so ask the server something trivial.
        ee.Number(1).getInfo()
    except Exception:
        return False
    return True


def initialise(project: str | None = None) -> None:
    """Initialise Earth Engine, authenticating interactively only if needed.

    Authentication opens a browser, so it is never triggered implicitly during
    catalog browsing -- only when the user actually asks to extract data.
    """
    ee = require_ee()
    try:
        ee.Initialize(project=project) if project else ee.Initialize()
    except Exception as first_error:
        LOGGER.info("ee.Initialize failed, authenticating: %s", first_error)
    else:
        return
    try:
        ee.Authenticate()
        ee.Initialize(project=project) if project else ee.Initialize()
    except Exception as error:
        raise EarthEngineUnavailable(
            f"Could not initialise Earth Engine: {error}\n\n"
            "Check that your Google account is registered for Earth Engine and, "
            "for recent API versions, that a Cloud project is set."
        ) from error


# ---------------------------------------------------------------------------
# Geometry bridging
# ---------------------------------------------------------------------------


def require_initialised():
    """Return ``ee``, or raise :class:`EarthEngineNotInitialised`.

    Every path that builds an ``ee`` object goes through here.  Constructing even
    an ``ee.Geometry`` contacts the server to load the API signatures, so there is
    no such thing as a safe "offline" ee call to let through unchecked.
    """
    ee = require_ee()
    if not is_initialised():
        raise EarthEngineNotInitialised(NOT_INITIALISED_ADVICE)
    return ee


def ring_to_ee(ring: Sequence[Sequence[float]]):
    """A ``(lon, lat)`` ring -> ``ee.Geometry.Polygon``."""
    ee = require_initialised()
    coordinates = [[float(x), float(y)] for x, y in ring]
    if coordinates and coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])
    if len(coordinates) < 4:
        raise EarthEngineError("A polygon needs at least three distinct corners")
    return _wrap(lambda: ee.Geometry.Polygon([coordinates]), "Building a polygon")


def bbox_to_ee(bbox: Sequence[float]):
    """``[west, south, east, north]`` -> ``ee.Geometry.Rectangle``."""
    ee = require_initialised()
    if not bbox or len(bbox) < 4:
        raise EarthEngineError(f"Expected [west, south, east, north], got {bbox!r}")
    west, south, east, north = (float(v) for v in bbox[:4])
    return _wrap(
        lambda: ee.Geometry.Rectangle([west, south, east, north]),
        "Building a rectangle",
    )


def geojson_to_ee(geojson: Mapping):
    """Any GeoJSON geometry / Feature / FeatureCollection -> ``ee.Geometry``.

    QGIS hands us multipart geometries, rings with holes and mixed collections.
    Several polygonal parts are merged into one ``MultiPolygon`` here, working
    purely on the GeoJSON, so building an area of interest costs no server calls;
    anything non-polygonal falls back to a ``GeometryCollection``.
    """
    ee = require_initialised()
    if not isinstance(geojson, Mapping):
        raise EarthEngineError("Expected a GeoJSON mapping")

    geometries = geometry.geometries_of(geojson)
    if not geometries:
        raise EarthEngineError("That GeoJSON contains no geometries")
    if len(geometries) == 1:
        return ee.Geometry(geometries[0])

    polygons: list[list] = []
    for geom in geometries:
        kind = geom.get("type")
        if kind == "Polygon":
            polygons.append(geom.get("coordinates") or [])
        elif kind == "MultiPolygon":
            polygons.extend(geom.get("coordinates") or [])
        else:
            polygons = []
            break
    if polygons:
        return ee.Geometry.MultiPolygon(polygons)
    return ee.Geometry({"type": "GeometryCollection", "geometries": geometries})


# ---------------------------------------------------------------------------
# Reading metadata
# ---------------------------------------------------------------------------


def _wrap(call, what: str):
    """Run an ``ee`` call, re-raising failures as :class:`EarthEngineError`."""
    try:
        return call()
    except Exception as error:
        raise EarthEngineError(f"{what}: {error}") from error


def describe(dataset_id: str, dataset_type: str) -> dict:
    """Fetch the server-side description of a dataset.

    Used for the details panel.  Image collections are sampled with ``.limit(1)``
    rather than ``.first()`` plus a date guess, so this is a single cheap call
    that cannot be defeated by a dataset whose first image sits outside the
    window the GUI happens to be showing.
    """
    ee = require_initialised()
    if dataset_type == "Image":
        return _wrap(lambda: ee.Image(dataset_id).getInfo(), f"Reading {dataset_id}")
    if dataset_type == "ImageCollection":
        collection = ee.ImageCollection(dataset_id)
        return _wrap(lambda: collection.limit(1).getInfo(), f"Reading {dataset_id}")
    if dataset_type == "FeatureCollection":
        collection = ee.FeatureCollection(dataset_id)
        return _wrap(lambda: collection.limit(1).getInfo(), f"Reading {dataset_id}")
    raise EarthEngineError(f"Unsupported dataset type {dataset_type!r}")


def band_names(info: Mapping) -> list[str]:
    """Pull band names out of whatever shape ``getInfo`` returned."""
    if not isinstance(info, Mapping):
        return []
    bands = info.get("bands")
    if not bands:
        features = info.get("features") or ()
        bands = features[0].get("bands") if features else None
    return [
        band["id"] for band in bands or () if isinstance(band, Mapping) and band.get("id")
    ]


def band_crs(info: Mapping, band_name: str) -> str | None:
    """The projection a specific band is stored in."""
    bands = info.get("bands") or (
        (info.get("features") or [{}])[0].get("bands") if info.get("features") else None
    )
    for band in bands or ():
        if isinstance(band, Mapping) and band.get("id") == band_name:
            return band.get("crs")
    return None


def footprint_bbox(info: Mapping) -> list[float] | None:
    """Derive ``[west, south, east, north]`` from a ``getInfo`` payload."""
    rings: list[list] = []
    features = info.get("features") if isinstance(info, Mapping) else None
    for feature in features or ([info] if isinstance(info, Mapping) else []):
        properties = feature.get("properties") or {}
        footprint = properties.get("system:footprint")
        if isinstance(footprint, Mapping) and footprint.get("coordinates"):
            coordinates = footprint["coordinates"]
            # Footprints come as either a ring or a list of rings.
            if coordinates and isinstance(coordinates[0][0], (int, float)):
                rings.append(coordinates)
            else:
                rings.extend(coordinates)
        else:
            # Some collections publish explicit corner properties instead.
            corners = ("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX")
            if all(key in properties for key in corners):
                rings.append(
                    [
                        [properties["LON_MIN"], properties["LAT_MIN"]],
                        [properties["LON_MAX"], properties["LAT_MAX"]],
                    ]
                )
    points = [point for ring in rings for point in ring if len(point) >= 2]
    if not points:
        return None
    return geometry.bbox_of([(float(p[0]), float(p[1])) for p in points])


# ---------------------------------------------------------------------------
# Running a request
# ---------------------------------------------------------------------------


def count_images(request: ExtractRequest) -> int:
    """How many images the filters actually select, server-side."""
    ee = require_initialised()
    if request.dataset_type != "ImageCollection":
        return 1
    collection = ee.ImageCollection(request.dataset_id).filterDate(
        request.start, request.end
    )
    if request.region is not None:
        collection = collection.filterBounds(request.region)
    return int(_wrap(lambda: collection.size().getInfo(), "Counting images"))


def extract(request: ExtractRequest) -> list[dict] | dict:
    """Run ``request`` and return rows (rasters) or GeoJSON (tables)."""
    ee = require_initialised()

    if request.dataset_type == "FeatureCollection":
        collection = ee.FeatureCollection(request.dataset_id)
        if request.region is not None:
            collection = collection.filterBounds(request.region)
        return _wrap(lambda: collection.getInfo(), f"Reading {request.dataset_id}")

    if request.dataset_type == "Image":
        image = ee.Image(request.dataset_id).select(list(request.bands))
        region = _region_for(request)
        rows = _wrap(
            lambda: image.sampleRectangle(region, defaultValue=0).getInfo(),
            f"Sampling {request.dataset_id}",
        )
        return _image_sample_to_rows(rows, request)

    collection = (
        ee.ImageCollection(request.dataset_id)
        .filterDate(request.start, request.end)
        .select(list(request.bands))
    )
    region = _region_for(request)
    table = _wrap(
        lambda: collection.getRegion(region, request.resolution_m, request.crs).getInfo(),
        f"Extracting {request.dataset_id}",
    )
    return region_table_to_rows(table, request)


def _region_for(request: ExtractRequest):
    if request.region is not None:
        return request.region
    if request.bbox:
        return bbox_to_ee(list(request.bbox))
    raise EarthEngineError("No area of interest set for this request")


def region_table_to_rows(
    table: Sequence[Sequence], request: ExtractRequest
) -> list[dict]:
    """``getRegion`` returns a header row followed by value rows; name the columns.

    Each row is annotated with the request parameters so a saved file is
    self-describing, and the timestamp embedded in the image id is expanded into
    readable start/stop columns.
    """
    if not table or len(table) < 2:
        return []
    columns = [str(name) for name in table[0]]
    rows: list[dict] = []
    for raw in table[1:]:
        row = dict(zip(columns, raw))
        identifier = row.get("id")
        if isinstance(identifier, str):
            parts = identifier.split("_")
            start = dates.image_index_time(parts[0]) if parts else None
            if start:
                row["image_start"] = start
            if len(parts) > 1:
                stop = dates.image_index_time(parts[1])
                if stop:
                    row["image_stop"] = stop
        if "time" in row and row["time"] is not None:
            readable = dates.parse(row["time"])
            if readable:
                row["time_utc"] = readable.strftime("%Y-%m-%d %H:%M:%S")
        row.update(
            {
                "dataset_id": request.dataset_id,
                "dataset_type": request.dataset_type,
                "resolution_m": request.resolution_m,
                "crs": request.crs,
                "filter_start": request.start,
                "filter_end": request.end,
                "filter_bands": ",".join(request.bands),
            }
        )
        rows.append(row)
    return rows


def _image_sample_to_rows(sample: Mapping, request: ExtractRequest) -> list[dict]:
    """Flatten ``sampleRectangle`` output into one row per pixel."""
    properties = (sample or {}).get("properties") or {}
    grids = {
        band: properties[band]
        for band in request.bands
        if isinstance(properties.get(band), list)
    }
    if not grids:
        return []
    first = next(iter(grids.values()))
    rows: list[dict] = []
    for y, line in enumerate(first):
        for x in range(len(line)):
            row = {"row": y, "column": x}
            for band, grid in grids.items():
                try:
                    row[band] = grid[y][x]
                except (IndexError, TypeError):
                    row[band] = None
            row.update(
                {
                    "dataset_id": request.dataset_id,
                    "dataset_type": request.dataset_type,
                    "resolution_m": request.resolution_m,
                    "filter_bands": ",".join(request.bands),
                }
            )
            rows.append(row)
    return rows
