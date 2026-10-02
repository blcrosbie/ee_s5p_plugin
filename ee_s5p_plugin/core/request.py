"""Describing, validating and sizing an extraction request.

``getRegion`` refuses to return more than :data:`VALUE_LIMIT` values, and the
original plugin only discovered this *after* a slow round trip, then tried to
parse the failure message back into advice.  The estimate here runs locally
before anything is sent, so the dock can warn -- and suggest a resolution that
would fit -- while the user is still choosing filters.

Pure Python: no Qt, no QGIS, no ``ee``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import dates, geometry

#: Hard ceiling ``ee.ImageCollection.getRegion`` enforces on returned values.
#: points x bands x images must stay under this.
VALUE_LIMIT = 1_048_576

#: Requests above this fraction of the limit get a warning rather than an error.
WARN_FRACTION = 0.8


class RequestError(ValueError):
    """Raised when a request cannot be built from the current selections."""


@dataclass
class Estimate:
    """A local prediction of how large a request will be."""

    points: int = 0
    bands: int = 1
    images: int = 0
    limit: int = VALUE_LIMIT

    @property
    def values(self) -> int:
        return max(0, self.points) * max(1, self.bands) * max(0, self.images)

    @property
    def fits(self) -> bool:
        return 0 < self.values <= self.limit

    @property
    def over_by(self) -> float:
        """How many times over the limit the request is (1.0 == exactly at it)."""
        return self.values / float(self.limit) if self.limit else 0.0

    @property
    def should_warn(self) -> bool:
        return self.values > self.limit * WARN_FRACTION

    def suggested_resolution(self, current_resolution_m: float) -> float | None:
        """Resolution that would bring the request under the limit.

        Point count scales with the inverse square of the pixel size, so the
        required scale factor is the square root of the overshoot.  Rounded up to
        a tidy number so the suggestion is something a person would actually type.
        """
        if self.fits or not current_resolution_m or current_resolution_m <= 0:
            return None
        needed = current_resolution_m * math.sqrt(self.over_by)
        # Round up to 2 significant figures.
        magnitude = 10 ** (math.floor(math.log10(needed)) - 1)
        return math.ceil(needed / magnitude) * magnitude

    def suggested_days(self, current_days: int | None) -> int | None:
        """Time window that would bring the request under the limit."""
        if self.fits or not current_days or current_days <= 0:
            return None
        return max(1, int(current_days / self.over_by))

    def describe(self) -> str:
        return (
            f"{self.points:,} points × {self.bands} band(s) "
            f"× {self.images:,} image(s) = {self.values:,} values "
            f"(limit {self.limit:,})"
        )


def format_metres(metres: float | None) -> str:
    """Render a resolution the way a person would write it.

    ``%g`` turns a coarse suggestion into ``1.2e+06 m``, which nobody would type
    into a resolution box.
    """
    if not metres:
        return ""
    if metres >= 1000:
        kilometres = metres / 1000.0
        text = f"{kilometres:,.0f}" if kilometres >= 10 else f"{kilometres:,.1f}"
        return f"{text} km"
    if metres >= 10:
        return f"{metres:,.0f} m"
    return f"{metres:g} m"


def format_days(days: int | None) -> str:
    """``1`` -> ``1 day``, ``7`` -> ``7 days``."""
    if not days:
        return ""
    return f"{days} day" if days == 1 else f"{days} days"


def estimate_image_collection(
    bbox: list[float] | tuple[float, ...],
    resolution_m: float,
    bands: int = 1,
    images: int = 1,
    limit: int = VALUE_LIMIT,
) -> Estimate:
    """Predict the value count for an ``ImageCollection.getRegion`` request."""
    if not bbox or len(bbox) < 4 or not resolution_m or resolution_m <= 0:
        return Estimate(points=0, bands=bands, images=images, limit=limit)
    area_km2 = geometry.bbox_area_km2(list(bbox))
    pixel_km2 = (resolution_m / 1000.0) ** 2
    points = int(area_km2 / pixel_km2) if pixel_km2 else 0
    return Estimate(
        points=points, bands=max(1, bands), images=max(0, images), limit=limit
    )


def estimate_images_in_window(start: object, end: object, interval: dict | None) -> int:
    """How many images a cadence implies over a date window.

    Falls back to one image per day, which is the common case for the daily L3
    products and errs on the safe side for anything coarser.
    """
    days = dates.day_span(start, end)
    if days is None:
        return 1
    days = max(1, days)
    if not isinstance(interval, dict):
        return days

    every = interval.get("interval") or 1
    unit = (interval.get("unit") or "day").lower()
    per_day = {
        "minute": 1440.0,
        "hour": 24.0,
        "day": 1.0,
        "week": 1.0 / 7.0,
        "month": 1.0 / 30.44,
        "year": 1.0 / 365.25,
    }.get(unit, 1.0)
    images = days * per_day / max(1, every)
    return max(1, math.ceil(images))


@dataclass
class ExtractRequest:
    """A validated description of one extraction, ready to be executed."""

    dataset_id: str
    dataset_type: str
    bands: tuple[str, ...] = ()
    start: str | None = None
    end: str | None = None
    resolution_m: float = 0.0
    crs: str | None = None
    #: ``[west, south, east, north]`` of the area of interest, for the estimate.
    bbox: tuple[float, float, float, float] | None = None
    #: Opaque area-of-interest object handed to ``ee`` (an ``ee.Geometry``).
    region: object | None = None
    interval: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise RequestError("No dataset selected")
        if self.dataset_type not in ("Image", "ImageCollection", "FeatureCollection"):
            raise RequestError(f"Unsupported dataset type {self.dataset_type!r}")

        if self.dataset_type in ("Image", "ImageCollection"):
            if not self.bands:
                raise RequestError("Select at least one band to extract")
            if not self.resolution_m or self.resolution_m <= 0:
                raise RequestError(
                    "Set a resolution in metres (the dataset's native "
                    "resolution is a good starting point)"
                )
        if self.dataset_type == "ImageCollection":
            if not self.start or not self.end:
                raise RequestError("An image collection needs a start and end date")
            span = dates.day_span(self.start, self.end)
            if span is not None and span < 0:
                raise RequestError("The end date is before the start date")

    @property
    def days(self) -> int | None:
        return dates.day_span(self.start, self.end)

    def estimate(self) -> Estimate:
        """Local size prediction; ``Estimate.points == 0`` means "unknown"."""
        if self.dataset_type == "FeatureCollection":
            return Estimate(points=0, bands=0, images=0)
        images = (
            estimate_images_in_window(self.start, self.end, self.interval)
            if self.dataset_type == "ImageCollection"
            else 1
        )
        return estimate_image_collection(
            list(self.bbox) if self.bbox else None,
            self.resolution_m,
            bands=len(self.bands) or 1,
            images=images,
        )

    def summary(self) -> str:
        lines = [f"{self.dataset_id}  ({self.dataset_type})"]
        if self.bands:
            lines.append(f"Bands: {', '.join(self.bands)}")
        if self.start or self.end:
            lines.append(f"Dates: {self.start or '?'} to {self.end or 'present'}")
        if self.resolution_m:
            lines.append(f"Resolution: {self.resolution_m:g} m")
        if self.crs:
            lines.append(f"CRS: {self.crs}")
        if self.bbox:
            west, south, east, north = self.bbox
            lines.append(
                f"Area: {west:.4f}, {south:.4f} to {east:.4f}, {north:.4f} "
                f"({geometry.bbox_area_km2(list(self.bbox)):,.0f} km²)"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interpreting Earth Engine failures
# ---------------------------------------------------------------------------

#: Substring -> plain-language explanation and what to try next.
_ERROR_HINTS = (
    (
        "too many values",
        "The request asks for more data than Earth Engine will return at once.",
        "Increase the resolution, shrink the area, narrow the dates, "
        "or extract one band at a time.",
    ),
    (
        "user memory limit exceeded",
        "Earth Engine ran out of memory assembling the result.",
        "Shrink the area of interest or the date range.",
    ),
    (
        "computation timed out",
        "Earth Engine gave up before the request finished.",
        "Shrink the area of interest or the date range and try again.",
    ),
    (
        "not found",
        "Earth Engine does not recognise that dataset id.",
        "Refresh the catalog -- the dataset may have been renamed or retired.",
    ),
    (
        "no such band",
        "That band is not present in the images the filters selected.",
        "Re-read the dataset details; bands can differ between versions.",
    ),
    (
        "not initialized",
        "Earth Engine has not been signed in to in this QGIS session.",
        "Sign in from the QGIS Python Console with ee.Authenticate(), then "
        "restart QGIS. See the plugin's README for the full sequence.",
    ),
    (
        "authorize access",
        "Earth Engine has no valid credentials for this machine.",
        "Run ee.Authenticate() in the QGIS Python Console. Credentials older "
        "than about 2023 are in a format the current API cannot use and have to "
        "be replaced.",
    ),
    (
        "no project found",
        "Earth Engine needs a Google Cloud project, and none is set.",
        "Pass one to ee.Initialize(project='your-cloud-project'), or set it in "
        "the Google Earth Engine plugin's settings.",
    ),
    (
        "caller does not have permission",
        "Your Cloud project is not registered for Earth Engine.",
        "Register it at https://code.earthengine.google.com/register, or pick a "
        "project that already is.",
    ),
    (
        "permission",
        "Your Earth Engine account cannot read this dataset.",
        "Check that your account is registered for Earth Engine and, for "
        "restricted datasets, that you have accepted the terms of use.",
    ),
    (
        "quota",
        "You have hit an Earth Engine usage quota.",
        "Wait a few minutes before retrying, and prefer smaller requests.",
    ),
)


def explain_error(error: BaseException | str) -> tuple[str, str]:
    """Turn an Earth Engine exception into ``(what happened, what to try)``."""
    text = str(error)
    lowered = text.lower()
    for needle, what, advice in _ERROR_HINTS:
        if needle in lowered:
            return what, advice
    return text, "Adjust the filters and try again."


def parse_limit_error(error: BaseException | str) -> Estimate | None:
    """Recover the real numbers from a "Too many values" message.

    Earth Engine words it as ``Too many values: 658739 points x 1 bands x
    4 images > 1048576.``  Parsing it gives an exact estimate to replace the
    local approximation, so the advice we show is based on real figures.
    """
    text = str(error)
    if "too many values" not in text.lower():
        return None
    try:
        body = text.lower().split("too many values", 1)[1].lstrip(": ")
        expression, _, limit_text = body.partition(">")
        limit = int(float(limit_text.strip().rstrip(".")))
        counts = {"points": 0, "bands": 1, "images": 0}
        for term in expression.split("x"):
            parts = term.strip().rstrip(".").split()
            if len(parts) >= 2 and parts[1] in counts:
                counts[parts[1]] = int(parts[0])
        if not counts["points"]:
            return None
        return Estimate(limit=limit, **counts)
    except (IndexError, ValueError):
        return None
