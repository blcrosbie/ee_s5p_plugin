"""Everything that touches the QGIS map: layers, geometry, styling.

Kept apart from the dock widget so the dock stays wiring, and so these can be
reasoned about on their own -- several of them were previously free functions
sitting under the widget class, one of which referenced ``self`` from module
scope and would have raised ``NameError`` the first time it ran.
"""

from __future__ import annotations

import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsGradientColorRamp,
    QgsGradientStop,
    QgsGraduatedSymbolRenderer,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSymbol,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor

from ..core import geometry as geom

WGS84 = "EPSG:4326"

#: Optional XYZ basemap, offered rather than forced.  v0.1 silently added a
#: Google tile layer on every start-up, which is both a surprise and a terms-of-
#: service question the user should answer for themselves.
BASEMAP_NAME = "OpenStreetMap"
BASEMAP_URI = (
    "type=xyz&zmin=0&zmax=19&url="
    "https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
)


class LayerError(RuntimeError):
    """Raised when a layer cannot be created or styled."""


# ---------------------------------------------------------------------------
# Basemap
# ---------------------------------------------------------------------------


def find_basemap():
    """An existing XYZ/WMS raster layer in the project, if there is one."""
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsRasterLayer) and layer.providerType() == "wms":
            return layer
    return None


def add_basemap(name: str = BASEMAP_NAME, uri: str = BASEMAP_URI):
    """Add a tiled basemap, returning the layer (or raising :class:`LayerError`)."""
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        raise LayerError(f"Could not load the {name} basemap")
    QgsProject.instance().addMapLayer(layer)
    return layer


# ---------------------------------------------------------------------------
# Reading an area of interest out of the project
# ---------------------------------------------------------------------------


def _to_wgs84_transform(source_crs):
    return QgsCoordinateTransform(
        source_crs,
        QgsCoordinateReferenceSystem(WGS84),
        QgsProject.instance(),
    )


def layers_with_selection(iface=None) -> list:
    """Every vector layer in the project with features selected, active first.

    Looking only at the active layer is a trap: the layer you select features on
    and the layer that happens to be highlighted in the Layers panel are routinely
    different, and the result was an area of interest that silently stayed unset.
    """
    active = iface.activeLayer() if iface else None
    found = [
        layer
        for layer in QgsProject.instance().mapLayers().values()
        if isinstance(layer, QgsVectorLayer) and layer.selectedFeatureCount() > 0
    ]
    # Active layer first, so a deliberate choice still wins; then by name, so the
    # result does not depend on QGIS's map ordering.
    found.sort(key=lambda layer: (layer is not active, layer.name()))
    return found


def describe_selection(iface=None) -> str:
    """What is selected and where, for telling the user what was used."""
    selected = layers_with_selection(iface)
    if not selected:
        return "nothing is selected on any vector layer"
    parts = [f"{layer.selectedFeatureCount()} on '{layer.name()}'" for layer in selected]
    return ", ".join(parts)


def selected_geometry_as_geojson(iface) -> dict | None:
    """Selected features from any vector layer, as WGS84 GeoJSON.

    Returns ``None`` when nothing is selected anywhere.  Each layer is reprojected
    with its *own* transform, since a selection can span layers in different CRSs.
    Reprojecting at all is the part v0.1 missed: it read raw layer coordinates and
    handed them to Earth Engine as degrees, so any projected layer produced an area
    of interest in the wrong place.
    """
    import json

    parsed: list[dict] = []
    for layer in layers_with_selection(iface):
        transform = _to_wgs84_transform(layer.crs())
        needs_transform = layer.crs().authid() != WGS84
        for feature in layer.selectedFeatures():
            geometry = QgsGeometry(feature.geometry())
            if geometry.isEmpty():
                continue
            if needs_transform and geometry.transform(transform) != 0:
                continue
            as_json = geometry.asJson()
            if as_json:
                parsed.append(json.loads(as_json))

    if not parsed:
        return None
    if len(parsed) == 1:
        return parsed[0]
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": g, "properties": {}} for g in parsed
        ],
    }


def canvas_extent_bbox(iface) -> list[float] | None:
    """The visible canvas extent as ``[west, south, east, north]`` in WGS84."""
    if iface is None:
        return None
    canvas = iface.mapCanvas()
    extent = canvas.extent()
    if extent.isEmpty():
        return None
    source = canvas.mapSettings().destinationCrs()
    if source.authid() != WGS84:
        transform = _to_wgs84_transform(source)
        try:
            extent = transform.transformBoundingBox(extent)
        except Exception as error:
            raise LayerError(f"Could not reproject the canvas extent: {error}") from error
    return [
        extent.xMinimum(),
        extent.yMinimum(),
        extent.xMaximum(),
        extent.yMaximum(),
    ]


def geojson_bbox(geojson: dict) -> list[float] | None:
    """``[west, south, east, north]`` of any GeoJSON object."""
    points: list[tuple[float, float]] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "FeatureCollection":
                for feature in node.get("features") or ():
                    walk(feature)
            elif node.get("type") == "Feature":
                walk(node.get("geometry"))
            elif node.get("type") == "GeometryCollection":
                for inner in node.get("geometries") or ():
                    walk(inner)
            else:
                walk(node.get("coordinates"))
        elif isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                points.append((float(node[0]), float(node[1])))
            else:
                for inner in node:
                    walk(inner)

    walk(geojson)
    return geom.bbox_of(points) if points else None


# ---------------------------------------------------------------------------
# Drawing on the map
# ---------------------------------------------------------------------------


def _memory_layer(wkb: str, name: str):
    layer = QgsVectorLayer(f"{wkb}?crs={WGS84}", name, "memory")
    if not layer.isValid():
        raise LayerError(f"Could not create the '{name}' scratch layer")
    return layer


def add_point(longitude: float, latitude: float, name: str = "Search location"):
    """Drop a marker, replacing any previous marker of the same name."""
    longitude, latitude = geom.validate_lonlat(longitude, latitude)
    remove_layers_named(name)
    layer = _memory_layer("Point", name)
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(longitude, latitude)))
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def add_polygon(ring, name: str = "Area of interest"):
    """Draw a closed ``(lon, lat)`` ring as a polygon layer."""
    remove_layers_named(name)
    layer = _memory_layer("Polygon", name)
    points = [QgsPointXY(float(x), float(y)) for x, y in ring]
    if points and points[0] != points[-1]:
        points.append(points[0])
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolygonXY([points]))
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def remove_layers_named(name: str) -> int:
    """Remove every layer with this exact name; returns how many went."""
    project = QgsProject.instance()
    doomed = [
        layer_id
        for layer_id, layer in project.mapLayers().items()
        if layer.name() == name
    ]
    for layer_id in doomed:
        project.removeMapLayer(layer_id)
    return len(doomed)


def add_vector_file(path: str, name: str | None = None):
    """Load a saved result file as a vector layer."""
    if not os.path.exists(path):
        raise LayerError(f"No such file: {path}")
    name = name or os.path.splitext(os.path.basename(path))[0]
    layer = QgsVectorLayer(path, name, "ogr")
    if not layer.isValid():
        raise LayerError(
            f"QGIS could not read {os.path.basename(path)} as a vector layer"
        )
    QgsProject.instance().addMapLayer(layer)
    return layer


def zoom_to(iface, bbox: list[float]) -> None:
    """Zoom the canvas to a WGS84 ``[west, south, east, north]`` box."""
    if iface is None or not bbox or len(bbox) < 4:
        return
    canvas = iface.mapCanvas()
    rectangle = QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3])
    destination = canvas.mapSettings().destinationCrs()
    if destination.authid() != WGS84:
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(WGS84), destination, QgsProject.instance()
        )
        try:
            rectangle = transform.transformBoundingBox(rectangle)
        except Exception:
            return
    canvas.setExtent(rectangle)
    canvas.refresh()


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------


def parse_colour(value: str) -> QColor | None:
    """Accept Earth Engine palette entries: ``ff0000``, ``#ff0000`` or ``red``.

    Earth Engine writes hex without the leading ``#``.  v0.1 tried to detect that
    with ``re.fullmatch("^[0-9a-fA-F]$", s)``, which only ever matches a *single*
    character, so every hex colour fell through to the "invalid" branch.
    """
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    if not text.startswith("#"):
        candidates.insert(0, "#" + text)
    for candidate in candidates:
        colour = QColor(candidate)
        if colour.isValid():
            return colour
    return None


def palette_colours(palette) -> list[QColor]:
    """Parse a palette, skipping entries Qt cannot interpret."""
    colours = [parse_colour(entry) for entry in palette or ()]
    return [colour for colour in colours if colour is not None]


def apply_graduated_style(
    layer,
    field: str,
    palette,
    classes: int = 0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> int:
    """Shade ``layer`` by ``field`` using an Earth Engine palette.

    Returns the number of classes created.  ``minimum``/``maximum`` default to the
    range actually present in the layer, so a palette designed for global values
    still shows contrast over a small area of interest.
    """
    if not isinstance(layer, QgsVectorLayer):
        raise LayerError("Select a vector layer to style")

    colours = palette_colours(palette)
    if len(colours) < 2:
        raise LayerError("That dataset has no usable colour palette")

    if field not in [f.name() for f in layer.fields()]:
        raise LayerError(
            f"The layer has no '{field}' field. Select the layer this "
            f"extraction created, then try again."
        )

    if minimum is None or maximum is None:
        values = []
        for feature in layer.getFeatures():
            value = feature[field]
            if value is None:
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        if not values:
            raise LayerError(f"No numeric values found in '{field}'")
        minimum = min(values) if minimum is None else minimum
        maximum = max(values) if maximum is None else maximum

    if maximum <= minimum:
        # A constant field cannot be graduated; widen it so the render succeeds.
        maximum = minimum + 1.0

    classes = classes or max(len(colours), 5)
    ramp = QgsGradientColorRamp(
        colours[0],
        colours[-1],
        stops=[
            QgsGradientStop(index / (len(colours) - 1), colour)
            for index, colour in enumerate(colours[1:-1], start=1)
        ],
    )
    renderer = QgsGraduatedSymbolRenderer.createRenderer(
        layer,
        field,
        classes,
        QgsGraduatedSymbolRenderer.Mode.EqualInterval,
        QgsSymbol.defaultSymbol(layer.geometryType()),
        ramp,
    )
    if renderer is None:
        raise LayerError("QGIS could not build a graduated renderer for that field")
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return (renderer.ranges() and len(renderer.ranges())) or classes
