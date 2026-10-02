#!/usr/bin/env python3
"""Load the plugin inside a real QGIS and drive the dock widget.

The unit tests cover ``core`` on plain Python.  This covers what they cannot: that
the plugin imports under this QGIS build's Qt version, that every widget can be
constructed, and that the filter/select/estimate paths run.  It is the check that
catches a Qt 5 only API slipping in.

    python3 tools/smoke_test.py            # needs a QGIS Python
    xvfb-run -a python3 tools/smoke_test.py   # headless Linux

On Windows with OSGeo4W::

    C:\\OSGeo4W\\bin\\python-qgis-ltr-qt6.bat tools\\smoke_test.py

Exits non-zero on the first failure.
"""

from __future__ import annotations

import os
import sys
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

try:
    from qgis.core import Qgis, QgsApplication
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import QT_VERSION_STR
    from qgis.PyQt.QtWidgets import QMainWindow
except ImportError as error:
    print(f"This script needs a QGIS Python environment: {error}", file=sys.stderr)
    raise SystemExit(3) from error


class FakeMessageBar:
    """Collects what the plugin would have shown the user."""

    def __init__(self):
        self.messages = []

    def pushMessage(self, title, message, level=0, duration=0):
        self.messages.append((title, message, level))


class FakeInterface:
    """The slice of QgisInterface the plugin actually uses."""

    def __init__(self):
        self._window = QMainWindow()
        self._canvas = QgsMapCanvas()
        self._bar = FakeMessageBar()
        self.toolbar_actions = []
        self.menu_actions = []
        self.docks = []

    def mainWindow(self):
        return self._window

    def mapCanvas(self):
        return self._canvas

    def messageBar(self):
        return self._bar

    def activeLayer(self):
        return None

    def addToolBarIcon(self, action):
        self.toolbar_actions.append(action)

    def removeToolBarIcon(self, action):
        self.toolbar_actions.remove(action)

    def addPluginToMenu(self, menu, action):
        self.menu_actions.append((menu, action))

    def removePluginMenu(self, menu, action):
        self.menu_actions = [e for e in self.menu_actions if e[1] is not action]

    def addDockWidget(self, area, widget):
        self._window.addDockWidget(area, widget)
        self.docks.append(widget)

    def removeDockWidget(self, widget):
        self._window.removeDockWidget(widget)
        if widget in self.docks:
            self.docks.remove(widget)


def check_styling(checks: Checks) -> None:
    """Apply an Earth Engine palette to a real layer.

    The palette parser is worth exercising against Qt: Earth Engine writes hex
    colours without a leading `#`, and v0.1's detection regex matched only a
    single character, so no hex palette was ever recognised.
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from ee_s5p_plugin.gui import layers

    checks.check(
        "bare hex palette entries are recognised",
        layers.parse_colour("ff0000") is not None
        and layers.parse_colour("ff0000").name() == "#ff0000",
    )
    checks.check(
        "named and prefixed colours are recognised",
        layers.parse_colour("black") is not None
        and layers.parse_colour("#1a9850") is not None,
    )
    checks.check("nonsense colours are rejected", layers.parse_colour("zzz") is None)

    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=NO2:double", "smoke", "memory")
    if not checks.check("scratch layer is valid", layer.isValid()):
        return
    features = []
    for index in range(10):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(index * 0.1, index * 0.1)))
        feature.setAttribute("NO2", index * 1e-5)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()

    palette = ["black", "blue", "purple", "cyan", "green", "yellow", "red"]
    try:
        classes = layers.apply_graduated_style(layer, "NO2", palette)
    except layers.LayerError as error:
        checks.check("graduated style applies", False, str(error))
        return
    checks.check("graduated style applies", classes == len(palette), f"{classes} classes")
    checks.check(
        "the renderer is attached to the layer",
        layer.renderer() is not None and len(layer.renderer().ranges()) == len(palette),
    )

    for label, field, pal in (
        ("a missing field is reported clearly", "NOPE", palette),
        ("an unusable palette is reported clearly", "NO2", ["zzz"]),
    ):
        try:
            layers.apply_graduated_style(layer, field, pal)
        except layers.LayerError:
            checks.check(label, True)
        else:
            checks.check(label, False, "no LayerError raised")


def check_autoscale(checks: Checks, dock) -> None:
    """Plan a tiled extraction, then run it against a stubbed Earth Engine.

    The planning arithmetic is unit tested; what needs a real QGIS is the dialog
    and the QgsTask that drives the chunks, including the adaptive subdivision
    when Earth Engine refuses a tile.
    """
    from ee_s5p_plugin.core import concurrency, hexgrid
    from ee_s5p_plugin.core import earthengine as ee_mod
    from ee_s5p_plugin.core import plan as plan_mod
    from ee_s5p_plugin.gui.autoscale import AutoScaleDialog
    from ee_s5p_plugin.gui.tasks import ChunkedExtractTask

    dataset = dock.catalog.get("COPERNICUS/S5P/OFFL/L3_NO2")
    if not checks.check("S5P NO2 is in the catalog for planning", dataset is not None):
        return

    # A US state sized area, which cannot be fetched in one request.
    state = {
        "type": "Polygon",
        "coordinates": [
            [
                [-80.52, 39.72],
                [-74.69, 39.72],
                [-74.69, 42.27],
                [-80.52, 42.27],
                [-80.52, 39.72],
            ]
        ],
    }
    grid = hexgrid.grid()
    checks.check(
        f"tiling grid available ({grid.describe()})",
        grid is not None,
    )

    bands = dataset.band_names[:3]
    try:
        built = plan_mod.plan(
            dataset,
            state,
            bands,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
            force_tiling=True,
        )
    except plan_mod.PlanError as error:
        checks.check("a state sized request can be planned", False, str(error))
        return

    checks.check(
        "the plan tiles the area",
        built.chunked and built.tile_count > 1,
        f"{built.tile_count} tiles at res {built.tile_resolution}",
    )
    checks.check(
        "every planned tile fits within the request limit",
        all(request_fits(tile, built) for tile in built.tiles),
    )
    checks.check(
        "more workers predicts a 2x or better speed-up",
        built.speedup(4) > 2.0,
        f"{built.speedup(4):.1f}x",
    )

    # -- the dialog -----------------------------------------------------
    dialog = AutoScaleDialog(
        built,
        replan=lambda res: plan_mod.plan(
            dataset,
            state,
            bands,
            "2024-06-01",
            "2024-07-01",
            resolution_m=1113.2,
            tile_grid=grid,
            force_tiling=True,
            minimum_resolution=res,
            maximum_resolution=res,
        ),
        parent=dock,
    )
    checks.check("auto-scale dialog offers tile sizes", dialog.resolutionCB.count() > 1)
    checks.check(
        "auto-scale dialog offers worker counts",
        dialog.workersCB.count() == len(concurrency.worker_choices()),
    )
    checks.check(
        "auto-scale dialog reports a plan", len(dialog.summary.toPlainText()) > 50
    )
    before = dialog.plan.tile_count
    finer = dialog.resolutionCB.findData((built.tile_resolution or 5) + 1)
    if finer >= 0:
        dialog.resolutionCB.setCurrentIndex(finer)
        checks.check(
            "choosing a finer tile size re-plans",
            dialog.plan.tile_count > before,
            f"{before} -> {dialog.plan.tile_count}",
        )
    checks.check("dialog reports a worker count", dialog.workers >= 1)
    dialog.close()

    # -- running it, with Earth Engine stubbed ---------------------------
    small = plan_mod.plan(
        dataset,
        state,
        bands,
        "2024-06-01",
        "2024-06-02",
        resolution_m=1113.2,
        tile_grid=grid,
        force_tiling=True,
        minimum_resolution=4,
        maximum_resolution=4,
    )

    original_extract = ee_mod.extract
    original_ring = ee_mod.ring_to_ee
    calls = []

    def fake_extract(request):
        calls.append(request.dataset_id)
        return [
            {"longitude": -80.0, "latitude": 40.0, bands[0]: 0.0001},
            {"longitude": -79.9, "latitude": 40.1, bands[0]: 0.0002},
        ]

    try:
        ee_mod.extract = fake_extract
        ee_mod.ring_to_ee = lambda ring: ("region", len(ring))

        for workers in (1, 4):
            calls.clear()
            task = ChunkedExtractTask(small, workers=workers, tile_grid=grid)
            ok = task.run()
            checks.check(
                f"chunked run succeeds with {concurrency.describe_workers(workers)}",
                ok,
                task._summary,
            )
            checks.check(
                f"every tile was fetched ({workers} worker(s))",
                len(calls) == small.tile_count,
                f"{len(calls)} of {small.tile_count}",
            )
            rows = task.data
            checks.check(
                f"rows collected ({workers} worker(s))",
                len(rows) == 2 * small.tile_count,
                f"{len(rows)} rows",
            )
            key = "h3_index" if grid.indexed else "tile_id"
            checks.check(
                f"every row carries its {key}",
                all(row.get(key) for row in rows),
            )
            checks.check(
                f"distinct tile ids are recorded ({workers} worker(s))",
                len({row[key] for row in rows}) == small.tile_count,
            )

        # -- a tile Earth Engine refuses must be subdivided and retried ----
        refused = {"n": 0}

        def picky_extract(request):
            # Refuse the first tile once, by its bounding box width.
            refused["n"] += 1
            if refused["n"] == 1:
                raise RuntimeError(
                    "Too many values: 9000000 points x 1 bands x 1 images > 1048576."
                )
            return [{"longitude": -80.0, "latitude": 40.0, bands[0]: 0.5}]

        ee_mod.extract = picky_extract
        task = ChunkedExtractTask(small, workers=1, tile_grid=grid)
        ok = task.run()
        checks.check("a refused tile is subdivided and retried", ok, task._summary)
        checks.check(
            "subdivision produced extra requests",
            refused["n"] > small.tile_count,
            f"{refused['n']} requests for {small.tile_count} tiles",
        )
        checks.check("data survived the subdivision", len(task.data) > 0)

        # -- a permanently failing tile is reported, not swallowed ---------
        def broken_extract(request):
            raise RuntimeError("Image.select: no such band 'nope'")

        ee_mod.extract = broken_extract
        task = ChunkedExtractTask(small, workers=1, tile_grid=grid)
        ok = task.run()
        checks.check("a permanent failure is reported as a failure", not ok)
        checks.check(
            "failures are listed for the user",
            len(task.failures) > 0,
            task.failures[0] if task.failures else "",
        )
    finally:
        ee_mod.extract = original_extract
        ee_mod.ring_to_ee = original_ring


def request_fits(tile, built) -> bool:
    from ee_s5p_plugin.core import request as request_mod

    estimate = request_mod.estimate_image_collection(
        tile.bbox, built.resolution_m, bands=len(built.bands), images=built.images
    )
    return estimate.fits


class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  ok   {label}")
        else:
            self.failures.append(f"{label}{': ' + detail if detail else ''}")
            print(f"  FAIL {label}{': ' + detail if detail else ''}")
        return bool(condition)


def run() -> int:
    print(
        f"QGIS {Qgis.QGIS_VERSION}  Qt {QT_VERSION_STR}  Python {sys.version.split()[0]}"
    )

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/usr"), True)
    app = QgsApplication([], False)
    app.initQgis()
    checks = Checks()

    try:
        import ee_s5p_plugin
        from ee_s5p_plugin.gui import details
        from ee_s5p_plugin.gui.catalog_model import DATASET_ROLE  # noqa: F401

        iface = FakeInterface()
        plugin = ee_s5p_plugin.classFactory(iface)
        plugin.initGui()
        checks.check("initGui registers one action", len(plugin.actions) == 1)

        plugin.show()
        dock = plugin.dockwidget
        if not checks.check("dock widget is created", dock is not None):
            return 1

        checks.check(
            "bundled catalog loads",
            len(dock.catalog) > 900,
            f"only {len(dock.catalog)} datasets",
        )
        checks.check(
            "deprecated datasets are hidden by default",
            dock.resultsModel.rowCount() < len(dock.catalog),
        )
        checks.check("type filter is populated", dock.typeCB.count() >= 3)
        checks.check(
            "collection presets are offered",
            dock.presetCB.count() >= 2,
        )

        # The Sentinel-5P preset is the plugin's original purpose and primary audience.
        from ee_s5p_plugin.gui.dockwidget import PRESETS

        s5p_index = next(
            (i for i, (label, _f) in enumerate(PRESETS) if "Sentinel-5P" in label), -1
        )
        if checks.check("a Sentinel-5P preset exists", s5p_index > 0):
            dock.presetCB.setCurrentIndex(s5p_index)
            rows = dock.resultsModel.rowCount()
            ids = [dock.resultsModel.dataset_at(r).id for r in range(rows)]
            checks.check(
                "the Sentinel-5P preset selects the S5P products",
                rows >= 18 and all("S5P" in i for i in ids),
                f"{rows} rows, e.g. {ids[:2]}",
            )
            dock.presetCB.setCurrentIndex(0)
            checks.check(
                "clearing the preset restores the whole catalog",
                dock.resultsModel.rowCount() > rows,
            )
            checks.check("provider filter is populated", dock.providerCB.count() > 50)

        # Text search
        dock.searchLE.setText("sentinel 5p no2")
        dock.apply_filters()
        checks.check(
            "text search narrows the list",
            0 < dock.resultsModel.rowCount() < 20,
            f"{dock.resultsModel.rowCount()} rows",
        )

        # Every filter control, to be sure each signal path runs
        dock.searchLE.setText("")
        for index in range(dock.typeCB.count()):
            dock.typeCB.setCurrentIndex(index)
        dock.typeCB.setCurrentIndex(0)
        for index in range(dock.timePresetCB.count()):
            dock.timePresetCB.setCurrentIndex(index)
        dock.timeFilterCB.setChecked(True)
        dock.timeFilterCB.setChecked(False)
        dock.bandsOnlyCB.setChecked(True)
        dock.bandsOnlyCB.setChecked(False)
        dock.deprecatedCB.setChecked(True)
        dock.deprecatedCB.setChecked(False)
        checks.check("every filter control runs without error", True)

        # Tags
        tag = dock.catalog.tags()[0]
        dock.tagLE.setText(tag)
        dock.add_tag()
        checks.check(
            f"tag filter '{tag}' applies",
            dock.tagList.count() == 1 and dock.resultsModel.rowCount() > 0,
        )
        dock.clear_tags()

        # Sorting on every column
        for column in range(dock.resultsModel.columnCount()):
            dock.resultsModel.sort(column)
        checks.check("results sort on every column", True)

        # Select the dataset the plugin was originally written for
        dock.searchLE.setText("COPERNICUS/S5P/OFFL/L3_NO2")
        dock.apply_filters()
        if checks.check(
            "the original S5P NO2 dataset is findable", dock.resultsModel.rowCount() >= 1
        ):
            dock.resultsView.setCurrentIndex(dock.resultsModel.index(0, 0))
            dataset = dock.selected_dataset()
            checks.check("selection returns a Dataset", dataset is not None)
            checks.check("bands populate on selection", dock.bandCB.count() > 1)
            checks.check(
                "native resolution is filled in",
                bool(dock.resolutionLE.text()),
                repr(dock.resolutionLE.text()),
            )
            checks.check(
                "details render as HTML", len(details.dataset_html(dataset)) > 1000
            )

            # Area of interest from coordinates, then the size estimate
            dock.lonLE.setText("-80.0")
            dock.latLE.setText("40.0")
            dock.radiusLE.setText("20")
            dock.build_area_of_interest()
            checks.check("area of interest is built", dock._aoi_bbox is not None)
            checks.check(
                "a small request is reported as fitting",
                "within" in dock.estimateLabel.text(),
                dock.estimateLabel.text(),
            )

            # The request window must be clamped to what the dataset covers:
            # the "All available" preset winds the start back to 1970.
            dock.timePresetCB.setCurrentIndex(0)
            request = dock.build_request()
            checks.check(
                "the request window is clamped to the dataset extent",
                request.start.startswith("2018"),
                f"start={request.start} end={request.end}",
            )
            checks.check(
                "the clamped window keeps the image count sane",
                request.estimate().images < 5000,
                f"{request.estimate().images} images",
            )

            # The canvas extent as an area of interest
            dock.use_canvas_as_area()
            checks.check(
                "canvas extent can be used as an area",
                dock._aoi_bbox is not None,
                dock.aoiLabel.text(),
            )

            # And that an oversized request is caught with advice
            dock.clear_area_of_interest()
            dock.resolutionLE.setText("10")
            checks.check(
                "an oversized request is flagged with a suggestion",
                "over the" in dock.estimateLabel.text()
                and "try about" in dock.estimateLabel.text(),
                dock.estimateLabel.text(),
            )

        check_styling(checks)
        check_autoscale(checks, dock)

        plugin.unload()
        checks.check("unload removes the action", len(iface.toolbar_actions) == 0)

    except Exception:
        traceback.print_exc()
        checks.failures.append("unhandled exception")
    finally:
        app.exitQgis()

    print()
    if checks.failures:
        total = checks.passed + len(checks.failures)
        print(f"FAILED ({len(checks.failures)} of {total})")
        for failure in checks.failures:
            print(f"  - {failure}")
        return 1
    print(f"PASSED ({checks.passed} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
