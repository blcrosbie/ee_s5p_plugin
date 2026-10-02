"""Tiling an area of interest into cells, for chunked extraction.

Earth Engine refuses a request that asks for too much at once, so a large area
has to be fetched as many smaller requests.  This module decides *what* those
pieces are.

`H3 <https://h3geo.org>`_ is the preferred tiling: its cells nest in a fixed
hierarchy, tile the globe with no gaps or overlaps, and carry a stable index, so
extracted rows can be joined against anything else keyed by H3 -- including
``h3-js`` on the web side.  ``h3`` is a compiled extension that QGIS does not
bundle, though, so it is an *optional* dependency: when it is missing we tile
with a plain latitude/longitude grid instead, which tiles just as exactly but has
no interoperable index.  Nothing here installs anything.

Resolutions behave as you would expect from H3: 3 is a region, 5 a county, 7 a
town, and each step finer divides a cell into roughly seven.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import geometry as geom

LOGGER = logging.getLogger(__name__)

#: Average H3 cell area in km², by resolution.  Matches
#: ``h3.average_hexagon_area(res, unit="km^2")`` and is kept here so a plan can
#: be costed, and a resolution chosen, with ``h3`` absent.
H3_AVERAGE_AREA_KM2 = {
    0: 4357449.416078383,
    1: 609788.4417941332,
    2: 86801.7803989972,
    3: 12393.43465508816,
    4: 1770.347654491307,
    5: 252.9038581819449,
    6: 36.12906216441245,
    7: 5.161293359717191,
    8: 0.7373275975944177,
    9: 0.1053325134272067,
    10: 0.01504750190766435,
    11: 0.002149643129451879,
    12: 0.000307091875631606,
    13: 4.387026794728296e-05,
    14: 6.267181135324313e-06,
    15: 8.95311590760579e-07,
}

FINEST_RESOLUTION = 15
COARSEST_RESOLUTION = 0

#: The band auto-scaling searches by default.  Coarser than 3 is a continent, and
#: finer than 9 means so many tiles that per-request overhead dominates.
DEFAULT_MIN_RESOLUTION = 3
DEFAULT_MAX_RESOLUTION = 9

#: Refuse to plan a job larger than this many tiles without the caller saying so;
#: at roughly a second per request, 20k tiles is most of a day.
DEFAULT_MAX_TILES = 20_000


class HexGridError(RuntimeError):
    """Raised when an area cannot be tiled."""


# ---------------------------------------------------------------------------
# Optional h3
# ---------------------------------------------------------------------------

_h3_module = None
_h3_checked = False


def h3_module():
    """The ``h3`` module, or ``None`` when it is not installed."""
    global _h3_module, _h3_checked
    if not _h3_checked:
        _h3_checked = True
        try:
            import h3
        except ImportError:
            _h3_module = None
        else:
            _h3_module = h3
    return _h3_module


def reset_h3_cache() -> None:
    """Forget whether ``h3`` was importable -- used by the tests."""
    global _h3_module, _h3_checked
    _h3_module, _h3_checked = None, False


def h3_available() -> bool:
    return h3_module() is not None


def h3_version() -> str | None:
    module = h3_module()
    return getattr(module, "__version__", "unknown") if module else None


def install_hint() -> str:
    """How to install ``h3`` into the Python *this QGIS* is running.

    Deliberately advice rather than action: a plugin that pip-installs into the
    QGIS environment behind the user's back can break their install, and the
    2020 version of this plugin did exactly that for BeautifulSoup.
    """
    import sys

    if sys.platform.startswith("win"):
        return (
            "Open the OSGeo4W Shell (or QGIS's own Python environment) and run:\n"
            "    python -m pip install h3\n\n"
            "On an OSGeo4W install that is typically:\n"
            "    C:\\OSGeo4W\\bin\\python-qgis-ltr.bat -m pip install h3"
        )
    return (
        "Install h3 into the Python that QGIS uses:\n"
        f"    {sys.executable} -m pip install h3\n\n"
        "Then restart QGIS."
    )


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tile:
    """One piece of a tiled area of interest."""

    id: str
    resolution: int
    #: Closed ring of ``(longitude, latitude)`` pairs.
    ring: tuple[tuple[float, float], ...]
    area_km2: float
    #: ``"h3"`` for a real H3 cell, ``"grid"`` for a fallback tile.
    source: str = "h3"

    @property
    def bbox(self) -> list[float]:
        return geom.bbox_of(list(self.ring))

    @property
    def is_indexed(self) -> bool:
        """Is :attr:`id` a real H3 index that other tools will understand?"""
        return self.source == "h3"

    def geojson(self) -> dict:
        return geom.ring_to_geojson(list(self.ring))


def _close(ring: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    points = [(float(x), float(y)) for x, y in ring]
    if points and points[0] != points[-1]:
        points.append(points[0])
    return tuple(points)


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------


class TileGrid:
    """Something that can cut an area of interest into tiles."""

    name = "grid"
    #: True when tile ids are meaningful outside this plugin.
    indexed = False

    def average_area_km2(self, resolution: int) -> float:
        try:
            return H3_AVERAGE_AREA_KM2[int(resolution)]
        except KeyError:
            raise HexGridError(
                f"Resolution {resolution} is outside "
                f"{COARSEST_RESOLUTION}..{FINEST_RESOLUTION}"
            ) from None

    def tiles_for(self, geojson: dict, resolution: int) -> list[Tile]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class H3Grid(TileGrid):
    """Real H3 cells, via the ``h3`` package.

    Supports both the 4.x API (``geo_to_cells``) and the 3.x one (``polyfill``),
    because which one a user has depends on how they installed it.
    """

    name = "h3"
    indexed = True

    def __init__(self):
        self._h3 = h3_module()
        if self._h3 is None:
            raise HexGridError("The h3 package is not installed")
        self._v4 = hasattr(self._h3, "geo_to_cells")

    def describe(self) -> str:
        return f"H3 {h3_version()}"

    def average_area_km2(self, resolution: int) -> float:
        resolution = int(resolution)
        getter = getattr(self._h3, "average_hexagon_area", None) or getattr(
            self._h3, "hex_area", None
        )
        if getter is not None:
            try:
                return float(getter(resolution, unit="km^2"))
            except Exception as error:
                # Falling back to the published table is fine, but swallowing the
                # reason silently would hide an h3 API change behind numbers that
                # merely look plausible.
                LOGGER.debug(
                    "h3 %s(%s) failed, using the published area table: %s",
                    getattr(getter, "__name__", "area getter"),
                    resolution,
                    error,
                )
        return super().average_area_km2(resolution)

    def _cells(self, geojson: dict, resolution: int) -> list[str]:
        if self._v4:
            return list(self._h3.geo_to_cells(geojson, resolution))
        # h3 3.x: coordinates are (lng, lat) only with geo_json_conformant.
        return list(self._h3.polyfill(geojson, resolution, geo_json_conformant=True))

    def _boundary(self, cell: str) -> tuple[tuple[float, float], ...]:
        if self._v4:
            # 4.x returns (lat, lng) pairs, unclosed.
            return _close([(lng, lat) for lat, lng in self._h3.cell_to_boundary(cell)])
        # 3.x with geo_json=True returns (lng, lat) pairs, unclosed.
        return _close(self._h3.h3_to_geo_boundary(cell, geo_json=True))

    def _area(self, cell: str, resolution: int) -> float:
        getter = getattr(self._h3, "cell_area", None)
        if getter is not None:
            try:
                return float(getter(cell, unit="km^2"))
            except Exception as error:
                LOGGER.debug(
                    "h3 cell_area(%s) failed, using the average for resolution %s: %s",
                    cell,
                    resolution,
                    error,
                )
        return self.average_area_km2(resolution)

    def tiles_for(self, geojson: dict, resolution: int) -> list[Tile]:
        resolution = int(resolution)
        tiles = []
        for part in polygonal_parts(geojson):
            try:
                cells = self._cells(part, resolution)
            except Exception as error:
                raise HexGridError(f"h3 could not tile that area: {error}") from error
            for cell in cells:
                tiles.append(
                    Tile(
                        id=str(cell),
                        resolution=resolution,
                        ring=self._boundary(cell),
                        area_km2=self._area(cell, resolution),
                        source="h3",
                    )
                )
        # Providers can overlap when an area is given as several polygons.
        unique: dict[str, Tile] = {}
        for tile in tiles:
            unique.setdefault(tile.id, tile)
        return list(unique.values())


class LatLonGrid(TileGrid):
    """Fallback tiling: a latitude/longitude grid over the area's extent.

    Used when ``h3`` is absent.  Tile ids are positional (``grid:5/3/7``) and mean
    nothing outside this plugin, but the tiles cover the extent exactly, with no
    gaps and no overlaps -- which is what matters for not double-counting or
    missing samples.  Tile *areas* track the H3 table so a plan costs the same
    either way.
    """

    name = "grid"
    indexed = False

    def describe(self) -> str:
        return "latitude/longitude grid (install h3 for real H3 indexes)"

    def tiles_for(self, geojson: dict, resolution: int) -> list[Tile]:
        resolution = int(resolution)
        target_area = self.average_area_km2(resolution)
        bbox = bbox_of_geojson(geojson)
        if bbox is None:
            raise HexGridError("That area of interest has no coordinates")

        west, south, east, north = bbox
        total = geom.bbox_area_km2(bbox)
        if total <= 0:
            raise HexGridError("That area of interest has no extent")

        # Split into a near-square number of rows and columns whose tiles come out
        # at about the target area.
        pieces = max(1, math.ceil(total / target_area))
        mean_lat = math.radians((north + south) / 2.0)
        width_deg = max(east - west, 1e-9)
        height_deg = max(north - south, 1e-9)
        aspect = (width_deg * max(math.cos(mean_lat), 1e-6)) / height_deg

        columns = max(1, round(math.sqrt(pieces * aspect)))
        rows = max(1, math.ceil(pieces / columns))

        step_x = width_deg / columns
        step_y = height_deg / rows

        tiles = []
        for row in range(rows):
            for column in range(columns):
                tile_west = west + column * step_x
                tile_south = south + row * step_y
                tile_bbox = [
                    tile_west,
                    tile_south,
                    min(tile_west + step_x, east),
                    min(tile_south + step_y, north),
                ]
                ring = _close(geom.bbox_polygon(tile_bbox))
                tiles.append(
                    Tile(
                        id=f"grid:{resolution}/{row}/{column}",
                        resolution=resolution,
                        ring=ring,
                        area_km2=geom.bbox_area_km2(tile_bbox),
                        source="grid",
                    )
                )
        return tiles


def grid(prefer_h3: bool = True) -> TileGrid:
    """The best tiling available: H3 when installed, the fallback otherwise."""
    if prefer_h3 and h3_available():
        try:
            return H3Grid()
        except HexGridError as error:
            LOGGER.info("Falling back to the grid tiling: %s", error)
    return LatLonGrid()


# ---------------------------------------------------------------------------
# GeoJSON helpers
# ---------------------------------------------------------------------------


def polygonal_parts(geojson: dict) -> list[dict]:
    """Flatten any GeoJSON container to its Polygon / MultiPolygon parts.

    Raises :class:`HexGridError` when nothing polygonal is present -- tiling a
    point or a line is meaningless, and ``h3`` reports it only as a ``ValueError``.
    """
    parts = [
        part
        for part in geom.geometries_of(geojson)
        if part.get("type") in ("Polygon", "MultiPolygon")
    ]
    if not parts:
        raise HexGridError(
            "An area of interest must be a polygon; got "
            + (
                ", ".join(
                    sorted({p.get("type", "?") for p in geom.geometries_of(geojson)})
                )
                or "nothing"
            )
        )
    return parts


def bbox_of_geojson(geojson: dict) -> list[float] | None:
    """``[west, south, east, north]`` of any GeoJSON object."""
    points: list[tuple[float, float]] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            walk(node.get("coordinates"))
            for key in ("geometries", "features"):
                for inner in node.get(key) or ():
                    walk(inner)
            if node.get("geometry"):
                walk(node["geometry"])
        elif isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                points.append((float(node[0]), float(node[1])))
            else:
                for inner in node:
                    walk(inner)

    walk(geojson)
    return geom.bbox_of(points) if points else None


# ---------------------------------------------------------------------------
# Choosing a resolution
# ---------------------------------------------------------------------------


def coarsest_resolution_for(
    area_budget_km2: float,
    minimum: int = DEFAULT_MIN_RESOLUTION,
    maximum: int = DEFAULT_MAX_RESOLUTION,
    areas: dict[int, float] | None = None,
) -> int | None:
    """The coarsest resolution whose average cell fits inside the budget.

    Coarse is better: fewer tiles means fewer requests and less overhead.
    Returns ``None`` when even the finest allowed resolution is too big, which
    means the ground resolution or band count has to come down instead.
    """
    if area_budget_km2 <= 0:
        return None
    areas = areas or H3_AVERAGE_AREA_KM2
    for resolution in range(int(minimum), int(maximum) + 1):
        if areas.get(resolution, float("inf")) <= area_budget_km2:
            return resolution
    return None


def estimate_tile_count(
    area_km2: float, resolution: int, areas: dict[int, float] | None = None
) -> int:
    """Roughly how many tiles an area needs at a resolution.

    Used to cost a plan before doing the real tiling, which for a large area at a
    fine resolution is itself slow.
    """
    areas = areas or H3_AVERAGE_AREA_KM2
    per_tile = areas.get(int(resolution))
    if not per_tile or area_km2 <= 0:
        return 0
    # Boundary cells overhang the area, so round up and add a perimeter margin.
    return max(1, math.ceil(area_km2 / per_tile * 1.15))


def resolution_table(
    minimum: int = COARSEST_RESOLUTION,
    maximum: int = FINEST_RESOLUTION,
) -> list[tuple[int, float]]:
    """``(resolution, average area km²)`` pairs, for showing the user."""
    return [
        (res, H3_AVERAGE_AREA_KM2[res])
        for res in range(int(minimum), int(maximum) + 1)
        if res in H3_AVERAGE_AREA_KM2
    ]


def describe_resolution(resolution: int) -> str:
    """A human sense of scale for an H3 resolution."""
    area = H3_AVERAGE_AREA_KM2.get(int(resolution))
    if area is None:
        return f"resolution {resolution}"
    comparisons = {
        0: "larger than most countries",
        1: "a large country",
        2: "a small country",
        3: "a region or large state",
        4: "a county",
        5: "a metropolitan area",
        6: "a city",
        7: "a town",
        8: "a neighbourhood",
        9: "a few city blocks",
        10: "a city block",
    }
    hint = comparisons.get(int(resolution), "very fine")
    if area >= 1:
        return f"res {resolution} \u2014 about {area:,.0f} km\u00b2 per cell ({hint})"
    return f"res {resolution} \u2014 about {area * 1e6:,.0f} m\u00b2 per cell ({hint})"


def tiles_covering(
    geojson: dict,
    resolution: int,
    tile_grid: TileGrid | None = None,
) -> list[Tile]:
    """Tile an area, guaranteeing at least one tile.

    H3 selects cells by *centre* containment, so an area smaller than a single
    cell produces no cells at all.  Left alone that is a silent "extracted
    nothing", so the area itself is used as a single tile in that case.
    """
    tile_grid = tile_grid or grid()
    tiles = tile_grid.tiles_for(geojson, resolution)
    if tiles:
        return tiles

    bbox = bbox_of_geojson(geojson)
    if bbox is None:
        raise HexGridError("That area of interest has no coordinates")
    return [
        Tile(
            id=f"aoi:{resolution}",
            resolution=resolution,
            ring=_close(geom.bbox_polygon(bbox)),
            area_km2=geom.bbox_area_km2(bbox),
            source="aoi",
        )
    ]


def total_area_km2(tiles: Iterable[Tile]) -> float:
    return sum(tile.area_km2 for tile in tiles)


def children_of(tile: Tile, tile_grid: TileGrid | None = None) -> list[Tile]:
    """Subdivide a tile one resolution finer.

    This is what makes adaptive refinement cheap: when a tile's request is
    rejected as too large at run time, its children can be retried without
    re-planning the whole job.  H3's hierarchy gives exactly seven children per
    cell (five around a pentagon); the fallback grid re-tiles the tile's extent.
    """
    finer = tile.resolution + 1
    if finer > FINEST_RESOLUTION:
        raise HexGridError(f"Resolution {tile.resolution} cannot be subdivided further")

    tile_grid = tile_grid or grid()
    if tile.source == "h3" and isinstance(tile_grid, H3Grid):
        module = tile_grid._h3
        getter = getattr(module, "cell_to_children", None) or getattr(
            module, "h3_to_children", None
        )
        if getter is not None:
            try:
                children = list(getter(tile.id, finer))
            except Exception as error:
                raise HexGridError(
                    f"h3 could not subdivide {tile.id}: {error}"
                ) from error
            return [
                Tile(
                    id=str(child),
                    resolution=finer,
                    ring=tile_grid._boundary(child),
                    area_km2=tile_grid._area(child, finer),
                    source="h3",
                )
                for child in children
            ]

    # Fallback (and 'aoi' tiles): re-tile this tile's own extent.
    return LatLonGrid().tiles_for(geom.ring_to_geojson(list(tile.ring)), finer)
