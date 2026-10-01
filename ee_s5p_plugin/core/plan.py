"""Planning a chunked extraction: how finely to tile, and how long it will take.

The user asks for a state and a band; Earth Engine will only answer a fraction of
that at once.  :func:`plan` works out the rest before anything is sent:

1. Would the whole area fit in one request?  If so, do not tile at all.
2. If not, what area *can* one request cover?  That is arithmetic, not guesswork:
   points scale as the inverse square of the ground resolution, so the per-tile
   area budget is ``limit x (resolution/1000)^2 / (bands x images)``.
3. Which is the coarsest tiling whose cells fit in that budget?  Coarse is better:
   fewer tiles means fewer round trips.
4. How many tiles is that, and how long will they take sequentially and in
   parallel?

Choosing the resolution arithmetically rather than by probing means the plan is
instant and costs no quota.  Earth Engine still has limits we cannot model --
memory, the 5,000 element cap on tables, odd collections -- so a tile that *is*
refused at run time gets subdivided and retried (see
:func:`~.concurrency.should_subdivide` and :func:`~.hexgrid.children_of`).  The
arithmetic gets us to roughly the right resolution in one step; refinement handles
the exceptions.

Pure Python: no Qt, no QGIS, no ``ee``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import concurrency, geometry, hexgrid
from . import request as request_mod
from .catalog import Dataset

#: Keep a margin under the hard limit: our image count is an estimate from the
#: published cadence, and a tile that lands slightly over wastes a round trip.
SAFETY_FRACTION = 0.7

#: ``getInfo`` on a table aborts past this many features, and feature density
#: cannot be predicted from metadata, so tables are tiled by area alone.
FEATURE_LIMIT = 5_000

#: Area per tile used for tables, absent anything better to go on.
TABLE_TILE_AREA_KM2 = hexgrid.H3_AVERAGE_AREA_KM2[5]


class PlanError(RuntimeError):
    """Raised when no workable plan exists."""


@dataclass
class Chunk:
    """One tile, and the request that will fetch it."""

    index: int
    tile: hexgrid.Tile
    request: request_mod.ExtractRequest

    @property
    def id(self) -> str:
        return self.tile.id


@dataclass
class Plan:
    """A costed extraction, ready to run."""

    dataset_id: str
    dataset_type: str
    bands: tuple[str, ...]
    start: str | None
    end: str | None
    resolution_m: float
    aoi_area_km2: float
    #: ``None`` when the whole area fits in one request and no tiling is needed.
    tile_resolution: int | None
    tiles: list[hexgrid.Tile] = field(default_factory=list)
    grid_name: str = "h3"
    indexed: bool = True
    per_tile: request_mod.Estimate | None = None
    whole_area: request_mod.Estimate | None = None
    images: int = 1
    seconds_per_request: float = concurrency.DEFAULT_SECONDS_PER_REQUEST
    notes: list[str] = field(default_factory=list)

    # -- shape ------------------------------------------------------------

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    @property
    def chunked(self) -> bool:
        return self.tile_resolution is not None and self.tile_count > 1

    @property
    def total_values(self) -> int:
        if self.per_tile is None:
            return 0
        return self.per_tile.values * max(1, self.tile_count)

    # -- timing -----------------------------------------------------------

    def seconds(self, workers: int = 1) -> float:
        return concurrency.estimate_seconds(
            max(1, self.tile_count), workers, self.seconds_per_request
        )

    def speedup(self, workers: int) -> float:
        return concurrency.speedup(
            max(1, self.tile_count), workers, self.seconds_per_request
        )

    def timing_options(
        self, choices: list[int] | None = None
    ) -> list[tuple[int, float, float]]:
        """``(workers, seconds, speedup)`` for each selectable worker count."""
        choices = choices or concurrency.worker_choices()
        return [
            (workers, self.seconds(workers), self.speedup(workers)) for workers in choices
        ]

    # -- presentation -----------------------------------------------------

    def describe(self, workers: int = 1) -> str:
        lines = [f"{self.dataset_id}  ({self.dataset_type})"]
        if self.bands:
            shown = ", ".join(self.bands[:4])
            if len(self.bands) > 4:
                shown += f" (+{len(self.bands) - 4} more)"
            lines.append(f"Bands: {shown}")
        if self.start or self.end:
            lines.append(
                f"Dates: {self.start or '?'} to {self.end or 'present'} "
                f"(~{self.images:,} images)"
            )
        lines.append(f"Resolution: {self.resolution_m:g} m")
        lines.append(f"Area: {self.aoi_area_km2:,.0f} km²")

        if not self.chunked:
            lines.append("Tiling: not needed, this fits in one request")
        else:
            lines.append(
                f"Tiling: {self.tile_count:,} tiles at "
                f"{hexgrid.describe_resolution(self.tile_resolution)}"
            )
            if self.per_tile is not None:
                lines.append(
                    f"Per tile: ~{self.per_tile.values:,} values "
                    f"(limit {self.per_tile.limit:,})"
                )
            lines.append(f"Grid: {self.grid_name}")

        lines.append(
            f"Estimated time: {concurrency.format_duration(self.seconds(workers))} "
            f"with {concurrency.describe_workers(workers)}"
        )
        if workers > 1:
            lines.append(
                f"  versus {concurrency.format_duration(self.seconds(1))} sequentially "
                f"({self.speedup(workers):.1f}x faster)"
            )
        lines.extend(f"Note: {note}" for note in self.notes)
        return "\n".join(lines)

    def chunks(self) -> list[Chunk]:
        """Build one request per tile.

        ``region`` is left unset: it is an ``ee`` object, and ``core`` does not
        import ``ee``.  The caller fills it in from :attr:`Chunk.tile`.
        """
        built = []
        for index, tile in enumerate(self.tiles):
            built.append(
                Chunk(
                    index=index,
                    tile=tile,
                    request=request_mod.ExtractRequest(
                        dataset_id=self.dataset_id,
                        dataset_type=self.dataset_type,
                        bands=self.bands,
                        start=self.start,
                        end=self.end,
                        resolution_m=self.resolution_m,
                        bbox=tuple(tile.bbox),
                    ),
                )
            )
        return built


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def tile_area_budget_km2(
    resolution_m: float,
    bands: int,
    images: int,
    limit: int = request_mod.VALUE_LIMIT,
    safety: float = SAFETY_FRACTION,
) -> float:
    """The largest area one request can cover, in km².

    ``values = area_km2 / (resolution_km)^2 x bands x images``, so solving for
    area gives ``limit x resolution_km^2 / (bands x images)``.
    """
    if resolution_m <= 0:
        raise PlanError("A ground resolution in metres is required")
    bands = max(1, int(bands))
    images = max(1, int(images))
    resolution_km = resolution_m / 1000.0
    return (limit * safety) * (resolution_km**2) / (bands * images)


def choose_tile_resolution(
    resolution_m: float,
    bands: int,
    images: int,
    minimum: int = hexgrid.DEFAULT_MIN_RESOLUTION,
    maximum: int = hexgrid.DEFAULT_MAX_RESOLUTION,
    limit: int = request_mod.VALUE_LIMIT,
    safety: float = SAFETY_FRACTION,
) -> tuple[int | None, float]:
    """``(resolution, area budget km²)`` -- the coarsest tiling that will fit."""
    budget = tile_area_budget_km2(resolution_m, bands, images, limit, safety)
    return hexgrid.coarsest_resolution_for(budget, minimum, maximum), budget


def required_resolution_m(
    tile_area_km2: float,
    bands: int,
    images: int,
    limit: int = request_mod.VALUE_LIMIT,
    safety: float = SAFETY_FRACTION,
) -> float:
    """The ground resolution needed to fit a tile of this area into one request.

    Used when even the finest allowed tiling is too big, to tell the user what
    would work instead of just refusing.
    """
    bands = max(1, int(bands))
    images = max(1, int(images))
    needed_km = math.sqrt(tile_area_km2 * bands * images / (limit * safety))
    return needed_km * 1000.0


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan(
    dataset: Dataset,
    aoi_geojson: dict,
    bands: tuple[str, ...] | list[str],
    start: str | None,
    end: str | None,
    resolution_m: float,
    minimum_resolution: int = hexgrid.DEFAULT_MIN_RESOLUTION,
    maximum_resolution: int = hexgrid.DEFAULT_MAX_RESOLUTION,
    max_tiles: int = hexgrid.DEFAULT_MAX_TILES,
    tile_grid: hexgrid.TileGrid | None = None,
    force_tiling: bool = False,
    seconds_per_request: float = concurrency.DEFAULT_SECONDS_PER_REQUEST,
) -> Plan:
    """Work out how to fetch ``aoi_geojson`` from ``dataset`` within the limits.

    Raises :class:`PlanError` when there is no workable plan, with a message that
    says what to change.
    """
    bands = tuple(bands)
    tile_grid = tile_grid or hexgrid.grid()

    aoi_bbox = hexgrid.bbox_of_geojson(aoi_geojson)
    if aoi_bbox is None:
        raise PlanError("The area of interest has no coordinates")
    aoi_area = geometry.bbox_area_km2(aoi_bbox)
    if aoi_area <= 0:
        raise PlanError("The area of interest has no extent")

    notes: list[str] = []
    if not tile_grid.indexed:
        notes.append(
            "h3 is not installed, so tiles are a plain latitude/longitude grid "
            "and their ids are not real H3 indexes"
        )

    built = Plan(
        dataset_id=dataset.id,
        dataset_type=dataset.type,
        bands=bands,
        start=start,
        end=end,
        resolution_m=float(resolution_m or 0),
        aoi_area_km2=aoi_area,
        tile_resolution=None,
        grid_name=tile_grid.describe(),
        indexed=tile_grid.indexed,
        seconds_per_request=seconds_per_request,
        notes=notes,
    )

    # -- tables: no band/resolution arithmetic applies -------------------
    if dataset.type == "FeatureCollection":
        return _plan_table(
            built,
            aoi_geojson,
            aoi_area,
            max_tiles,
            tile_grid,
            minimum_resolution,
            maximum_resolution,
            force_tiling,
        )

    if built.resolution_m <= 0:
        raise PlanError("Set a ground resolution in metres before planning")
    if not bands:
        raise PlanError("Select at least one band before planning")

    images = (
        request_mod.estimate_images_in_window(start, end, dataset.interval)
        if dataset.type == "ImageCollection"
        else 1
    )
    built.images = images

    # 1. Does the whole area fit as one request?
    whole = request_mod.estimate_image_collection(
        aoi_bbox, built.resolution_m, bands=len(bands), images=images
    )
    built.whole_area = whole
    if whole.fits and not whole.should_warn and not force_tiling:
        built.per_tile = whole
        built.tiles = [_aoi_tile(aoi_bbox, aoi_area)]
        return built

    # 2. How much area can one request cover, and which tiling fits in it?
    resolution, _budget = choose_tile_resolution(
        built.resolution_m,
        len(bands),
        images,
        minimum_resolution,
        maximum_resolution,
        safety=SAFETY_FRACTION,
    )
    if resolution is None:
        finest = hexgrid.H3_AVERAGE_AREA_KM2[maximum_resolution]
        needed = required_resolution_m(finest, len(bands), images)
        raise PlanError(
            f"Even the smallest tile offered ({finest:,.3f} km²) is too much "
            f"for one request at {built.resolution_m:g} m with {len(bands)} band(s) "
            f"over ~{images:,} images.\n\n"
            f"Try a ground resolution of about "
            f"{request_mod.format_metres(needed)}, fewer bands, or a shorter date "
            f"range."
        )

    # 3. Cost the tiling before doing it: tiling a continent at res 9 is itself slow.
    predicted = hexgrid.estimate_tile_count(aoi_area, resolution)
    if predicted > max_tiles:
        raise PlanError(
            f"That would need about {predicted:,} requests, over the "
            f"{max_tiles:,} limit.\n\n"
            f"Use a coarser ground resolution, fewer bands, a shorter date range, "
            f"or a smaller area."
        )

    built.tile_resolution = resolution
    built.tiles = hexgrid.tiles_covering(aoi_geojson, resolution, tile_grid)
    if not built.tiles:
        raise PlanError("That area could not be tiled")

    # Cost the *largest* tile actually produced, not the published average: that
    # is the one that decides whether any request gets refused.
    largest = max(tile.area_km2 for tile in built.tiles)
    built.per_tile = request_mod.estimate_image_collection(
        _square_bbox(largest),
        built.resolution_m,
        bands=len(bands),
        images=images,
    )
    if not built.per_tile.fits:
        built.notes.append(
            "some tiles may still be refused and will be split automatically"
        )

    if built.tile_count > max_tiles:
        raise PlanError(
            f"Tiling produced {built.tile_count:,} requests, over the "
            f"{max_tiles:,} limit.\n\nNarrow the area or the date range."
        )
    if not built.tiles:
        raise PlanError("That area could not be tiled")
    return built


def budget_area(resolution: int) -> float:
    return hexgrid.H3_AVERAGE_AREA_KM2[int(resolution)]


def _square_bbox(area_km2: float) -> list[float]:
    """A lon/lat box at the equator with roughly this area, for costing a tile."""
    side_km = math.sqrt(max(area_km2, 1e-12))
    half_deg = (side_km / 2.0) / (math.pi * geometry.EARTH_RADIUS_KM / 180.0)
    return [-half_deg, -half_deg, half_deg, half_deg]


def _aoi_tile(bbox: list[float], area_km2: float) -> hexgrid.Tile:
    """A single tile standing for the whole area, when no tiling is needed."""
    return hexgrid.Tile(
        id="aoi",
        resolution=-1,
        ring=tuple(geometry.bbox_polygon(bbox)),
        area_km2=area_km2,
        source="aoi",
    )


def _plan_table(
    built: Plan,
    aoi_geojson: dict,
    aoi_area: float,
    max_tiles: int,
    tile_grid: hexgrid.TileGrid,
    minimum_resolution: int,
    maximum_resolution: int,
    force_tiling: bool,
) -> Plan:
    """Tables are sized by feature count, which metadata cannot predict.

    So they are tiled by area alone when the area is large, and tiles that still
    come back over the 5,000 element cap are subdivided at run time.
    """
    built.notes.append(
        f"Tables are limited to {FEATURE_LIMIT:,} features per request and feature "
        "density is not published, so oversized tiles are split as they are found"
    )
    if aoi_area <= TABLE_TILE_AREA_KM2 and not force_tiling:
        built.tiles = [_aoi_tile(hexgrid.bbox_of_geojson(aoi_geojson), aoi_area)]
        return built

    resolution = hexgrid.coarsest_resolution_for(
        TABLE_TILE_AREA_KM2, minimum_resolution, maximum_resolution
    )
    if resolution is None:
        resolution = maximum_resolution
    predicted = hexgrid.estimate_tile_count(aoi_area, resolution)
    if predicted > max_tiles:
        raise PlanError(
            f"That area would need about {predicted:,} requests, over the "
            f"{max_tiles:,} limit. Use a smaller area."
        )
    built.tile_resolution = resolution
    built.tiles = hexgrid.tiles_covering(aoi_geojson, resolution, tile_grid)
    return built


# ---------------------------------------------------------------------------
# Refinement at run time
# ---------------------------------------------------------------------------


def subdivide(
    chunk: Chunk,
    tile_grid: hexgrid.TileGrid | None = None,
    start_index: int = 0,
) -> list[Chunk]:
    """Replace a rejected chunk with finer ones covering the same ground.

    Returns an empty list when the tile cannot usefully be divided -- either it is
    already at the finest resolution, or subdividing gives back a single tile of
    the same extent, which would loop forever.
    """
    tile_grid = tile_grid or hexgrid.grid()
    try:
        children = hexgrid.children_of(chunk.tile, tile_grid)
    except hexgrid.HexGridError:
        return []
    if not children:
        return []
    if len(children) == 1 and children[0].area_km2 >= chunk.tile.area_km2 * 0.99:
        return []

    out = []
    for offset, tile in enumerate(children):
        out.append(
            Chunk(
                index=start_index + offset,
                tile=tile,
                request=request_mod.ExtractRequest(
                    dataset_id=chunk.request.dataset_id,
                    dataset_type=chunk.request.dataset_type,
                    bands=chunk.request.bands,
                    start=chunk.request.start,
                    end=chunk.request.end,
                    resolution_m=chunk.request.resolution_m,
                    bbox=tuple(tile.bbox),
                ),
            )
        )
    return out


def tag_rows(rows: list[dict], tile: hexgrid.Tile) -> list[dict]:
    """Stamp each extracted row with the tile it came from.

    An H3 index is the useful part: it lets the output be joined against anything
    else keyed by H3, aggregated per cell, or re-fetched tile by tile.
    """
    key = "h3_index" if tile.source == "h3" else "tile_id"
    for row in rows:
        row[key] = tile.id
        row["tile_resolution"] = tile.resolution
    return rows
