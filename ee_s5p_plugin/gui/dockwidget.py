"""The dock widget: browse the catalog, then extract from the chosen dataset.

The interface is built in code rather than loaded from a ``.ui`` file.  The
original form placed every widget at absolute pixel coordinates inside a
269-pixel-wide dock, so it could not be resized, ignored the user's font size and
could not grow to hold a 1100-row result list.  Building it here keeps it fully
layout-managed, and removes a generated file that can silently drift from the code.

This class stays deliberately thin: filtering lives in
:mod:`...core.catalog`, sizing in :mod:`...core.request`, map work in
:mod:`..layers`, and network work in :mod:`..tasks`.
"""

from __future__ import annotations

import contextlib
import os

from qgis.core import Qgis, QgsApplication, QgsTask
from qgis.PyQt.QtCore import QDate, QItemSelectionModel, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QDoubleValidator
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core import (
    concurrency,
    dates,
    earthengine,
    export,
    geometry,
    hexgrid,
    store,
)
from ..core import plan as plan_mod
from ..core import progress as progress_mod
from ..core.catalog import Catalog, Dataset, Filters
from ..core.request import ExtractRequest, RequestError, format_days, format_metres
from . import details, layers, messages
from .autoscale import AutoScaleDialog
from .catalog_model import COLUMN_DATASET, DATASET_ROLE, DatasetTableModel
from .progress import ProgressPanel
from .tasks import CatalogRefreshTask, ChunkedExtractTask, ExtractTask

#: Named windows offered in the time tab.
TIME_PRESETS = (
    ("All available", None),
    ("Last 24 hours", ("days", 1)),
    ("Last 7 days", ("days", 7)),
    ("Last 30 days", ("days", 30)),
    ("Last 3 months", ("months", 3)),
    ("Last 6 months", ("months", 6)),
    ("Last year", ("months", 12)),
    ("Year to date", ("ytd", 0)),
)

#: One-click groups, by catalog tag.  Sentinel-5P comes first: it is what the
#: plugin was built for and remains the common case.  The default is the whole
#: catalog, and the last choice is remembered between sessions.
PRESETS: tuple[tuple[str, dict], ...] = (
    ("All datasets", {}),
    ("Sentinel-5P / TROPOMI", {"tags": ("s5p",)}),
    ("Air quality & atmosphere", {"tags": ("atmosphere",)}),
    ("Climate & weather", {"tags": ("climate", "weather")}),
    ("Elevation & land cover", {"tags": ("elevation", "landcover")}),
)

#: Where the chosen preset is remembered.
SETTINGS_PRESET = "ee_s5p_plugin/preset"

#: How long after the last keystroke the search re-runs.
SEARCH_DEBOUNCE_MS = 200

ALL_BANDS = "— all bands —"


class EarthEngineCatalogDockWidget(QDockWidget):
    """Browse the Earth Engine Data Catalog and extract data into QGIS."""

    closingPlugin = pyqtSignal()

    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(self.tr("Earth Engine Catalog"))
        self.setObjectName("EarthEngineCatalogDock")

        self.catalog: Catalog = Catalog()
        self._aoi_geojson: dict | None = None
        self._aoi_bbox: list[float] | None = None
        self._last_result: object = None
        self._last_request: ExtractRequest | None = None
        self._tasks: list = []

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self.apply_filters)

        self._build_ui()
        self._connect_signals()
        self.restore_preset()
        self.load_catalog()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        contents = QWidget(self)
        outer = QVBoxLayout(contents)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        outer.addLayout(self._build_search_row())
        outer.addLayout(self._build_preset_row())
        outer.addWidget(self._build_filter_tabs())
        outer.addLayout(self._build_results_header())
        outer.addWidget(self._build_results_view(), stretch=1)
        outer.addWidget(self._build_extract_group())

        self.progressPanel = ProgressPanel(contents)
        self.progressPanel.cancelRequested.connect(self.cancel_current_task)
        outer.addWidget(self.progressPanel)

        self.setWidget(contents)

    def _build_search_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.searchLE = QLineEdit()
        self.searchLE.setPlaceholderText(self.tr("Search the catalog…"))
        self.searchLE.setClearButtonEnabled(True)
        self.searchLE.setToolTip(
            self.tr(
                "Matches dataset id, title, publisher, tags, band names and "
                "description. Several words must all match."
            )
        )
        row.addWidget(self.searchLE, stretch=1)

        self.refreshBTN = QToolButton()
        self.refreshBTN.setText("↻")
        self.refreshBTN.setToolTip(self.tr("Refresh the catalog from Google"))
        row.addWidget(self.refreshBTN)
        return row

    def _build_preset_row(self) -> QHBoxLayout:
        """One-click jumps to the collections this plugin is most used for.

        The Sentinel-5P products are what the plugin was written for and remain
        the common case, so they get a shortcut rather than needing the right
        search term.
        """
        row = QHBoxLayout()
        row.addWidget(QLabel(self.tr("Collection")))
        self.presetCB = QComboBox()
        self.presetCB.setToolTip(
            self.tr("Jump to a group of related datasets, or browse everything")
        )
        for label, _filters in PRESETS:
            self.presetCB.addItem(label)
        row.addWidget(self.presetCB, stretch=1)
        return row

    def _build_filter_tabs(self) -> QTabWidget:
        self.filterTabs = QTabWidget()
        self.filterTabs.addTab(self._build_type_tab(), self.tr("Type"))
        self.filterTabs.addTab(self._build_tags_tab(), self.tr("Tags"))
        self.filterTabs.addTab(self._build_time_tab(), self.tr("Time"))
        self.filterTabs.addTab(self._build_area_tab(), self.tr("Area"))
        return self.filterTabs

    def _build_type_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(6, 6, 6, 6)

        self.typeCB = QComboBox()
        self.typeCB.setDuplicatesEnabled(False)
        form.addRow(self.tr("Dataset type"), self.typeCB)

        self.providerCB = QComboBox()
        self.providerCB.setDuplicatesEnabled(False)
        form.addRow(self.tr("Provider"), self.providerCB)

        self.bandsOnlyCB = QCheckBox(self.tr("Only datasets with bands"))
        form.addRow(self.bandsOnlyCB)

        self.deprecatedCB = QCheckBox(self.tr("Include deprecated datasets"))
        self.deprecatedCB.setToolTip(
            self.tr(
                "Google keeps serving deprecated datasets but no longer updates "
                "them. Shown struck through when included."
            )
        )
        form.addRow(self.deprecatedCB)
        return page

    def _build_tags_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)

        self.tagLE = QLineEdit()
        self.tagLE.setPlaceholderText(self.tr("Add a tag and press Enter…"))
        layout.addWidget(self.tagLE)

        self.tagList = QListWidget()
        self.tagList.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tagList.setMaximumHeight(90)
        self.tagList.setToolTip(self.tr("Select a tag and press Delete to remove it"))
        layout.addWidget(self.tagList)

        mode = QHBoxLayout()
        self.tagAnyRB = QRadioButton(self.tr("Any"))
        self.tagAllRB = QRadioButton(self.tr("All"))
        self.tagAnyRB.setChecked(True)
        mode.addWidget(self.tagAnyRB)
        mode.addWidget(self.tagAllRB)
        mode.addStretch(1)
        self.tagClearBTN = QPushButton(self.tr("Clear"))
        mode.addWidget(self.tagClearBTN)
        layout.addLayout(mode)
        return page

    def _build_time_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(6, 6, 6, 6)

        self.timePresetCB = QComboBox()
        for label, _ in TIME_PRESETS:
            self.timePresetCB.addItem(label)
        form.addRow(self.tr("Window"), self.timePresetCB)

        self.startDE = QDateEdit()
        self.startDE.setDisplayFormat("yyyy-MM-dd")
        self.startDE.setCalendarPopup(True)
        self.startDE.setMinimumDate(QDate(1970, 1, 1))
        self.startDE.setMaximumDate(QDate.currentDate())
        self.startDE.setDate(QDate(1970, 1, 1))
        form.addRow(self.tr("From"), self.startDE)

        self.endDE = QDateEdit()
        self.endDE.setDisplayFormat("yyyy-MM-dd")
        self.endDE.setCalendarPopup(True)
        self.endDE.setMinimumDate(QDate(1970, 1, 1))
        self.endDE.setMaximumDate(QDate.currentDate())
        self.endDE.setDate(QDate.currentDate())
        form.addRow(self.tr("To"), self.endDE)

        self.timeFilterCB = QCheckBox(self.tr("Filter results by this window"))
        self.timeFilterCB.setChecked(False)
        self.timeFilterCB.setToolTip(
            self.tr(
                "When off, the dates are used only for extraction, not to hide "
                "datasets from the list."
            )
        )
        form.addRow(self.timeFilterCB)
        return page

    def _build_area_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        grid = QGridLayout()
        self.lonLE = QLineEdit()
        self.lonLE.setValidator(QDoubleValidator(-180.0, 180.0, 8, self))
        self.lonLE.setPlaceholderText("-80.0")
        self.latLE = QLineEdit()
        self.latLE.setValidator(QDoubleValidator(-90.0, 90.0, 8, self))
        self.latLE.setPlaceholderText("40.0")
        grid.addWidget(QLabel(self.tr("Lon")), 0, 0)
        grid.addWidget(self.lonLE, 0, 1)
        grid.addWidget(QLabel(self.tr("Lat")), 0, 2)
        grid.addWidget(self.latLE, 0, 3)

        self.radiusLE = QLineEdit()
        self.radiusLE.setValidator(QDoubleValidator(0.0001, 20000.0, 4, self))
        self.radiusLE.setPlaceholderText("10")
        grid.addWidget(QLabel(self.tr("Size")), 1, 0)
        grid.addWidget(self.radiusLE, 1, 1)

        self.unitCB = QComboBox()
        self.unitCB.addItems(["km", "mi", "m"])
        grid.addWidget(self.unitCB, 1, 2)

        self.shapeCB = QComboBox()
        self.shapeCB.addItems([self.tr("Square"), self.tr("Hexagon")])
        grid.addWidget(self.shapeCB, 1, 3)
        layout.addLayout(grid)

        buttons = QGridLayout()
        self.buildAoiBTN = QPushButton(self.tr("Draw area"))
        self.useSelectionBTN = QPushButton(self.tr("Use selection"))
        self.useCanvasBTN = QPushButton(self.tr("Use canvas"))
        self.clearAoiBTN = QPushButton(self.tr("Clear"))
        self.useSelectionBTN.setToolTip(
            self.tr("Use the selected features of the active vector layer")
        )
        self.useCanvasBTN.setToolTip(self.tr("Use the visible map extent"))
        buttons.addWidget(self.buildAoiBTN, 0, 0)
        buttons.addWidget(self.useSelectionBTN, 0, 1)
        buttons.addWidget(self.useCanvasBTN, 1, 0)
        buttons.addWidget(self.clearAoiBTN, 1, 1)
        layout.addLayout(buttons)

        self.aoiLabel = QLabel(self.tr("No area of interest set"))
        self.aoiLabel.setWordWrap(True)
        layout.addWidget(self.aoiLabel)
        return page

    def _build_results_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.resultsLabel = QLabel()
        self.resultsLabel.setWordWrap(True)
        row.addWidget(self.resultsLabel, stretch=1)
        self.detailsBTN = QPushButton(self.tr("Details…"))
        self.detailsBTN.setEnabled(False)
        row.addWidget(self.detailsBTN)
        return row

    def _build_results_view(self) -> QTreeView:
        self.resultsModel = DatasetTableModel(self)
        self.resultsView = QTreeView()
        self.resultsView.setModel(self.resultsModel)
        self.resultsView.setRootIsDecorated(False)
        self.resultsView.setAlternatingRowColors(True)
        self.resultsView.setSortingEnabled(True)
        self.resultsView.setUniformRowHeights(True)  # keeps 1100 rows scrolling fast
        self.resultsView.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.resultsView.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.resultsView.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.resultsView.setMinimumHeight(140)
        self.resultsView.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        header = self.resultsView.header()
        header.setSectionResizeMode(COLUMN_DATASET, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.resultsModel.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        return self.resultsView

    def _build_extract_group(self) -> QGroupBox:
        group = QGroupBox(self.tr("Extract"))
        form = QFormLayout(group)
        form.setContentsMargins(6, 6, 6, 6)

        self.bandCB = QComboBox()
        self.bandCB.setDuplicatesEnabled(False)
        form.addRow(self.tr("Band"), self.bandCB)

        resolution_row = QHBoxLayout()
        self.resolutionLE = QLineEdit()
        self.resolutionLE.setValidator(QDoubleValidator(0.1, 1_000_000.0, 4, self))
        self.resolutionLE.setPlaceholderText(self.tr("metres per pixel"))
        resolution_row.addWidget(self.resolutionLE, stretch=1)
        self.nativeResBTN = QToolButton()
        self.nativeResBTN.setText(self.tr("Native"))
        self.nativeResBTN.setToolTip(self.tr("Use the dataset's own ground resolution"))
        resolution_row.addWidget(self.nativeResBTN)
        form.addRow(self.tr("Resolution"), resolution_row)

        self.estimateLabel = QLabel()
        self.estimateLabel.setWordWrap(True)
        form.addRow(self.estimateLabel)

        actions = QHBoxLayout()
        self.extractBTN = QPushButton(self.tr("Extract…"))
        self.extractBTN.setEnabled(False)
        self.styleBTN = QPushButton(self.tr("Style layer"))
        self.styleBTN.setEnabled(False)
        self.styleBTN.setToolTip(
            self.tr("Shade the active layer using the dataset's palette")
        )
        actions.addWidget(self.extractBTN, stretch=1)
        actions.addWidget(self.styleBTN)
        form.addRow(actions)
        return group

    def _connect_signals(self) -> None:
        self.searchLE.textChanged.connect(self._search_timer.start)
        self.refreshBTN.clicked.connect(self.refresh_catalog)
        self.presetCB.currentIndexChanged.connect(self.apply_preset)

        self.typeCB.currentIndexChanged.connect(self.apply_filters)
        self.providerCB.currentIndexChanged.connect(self.apply_filters)
        self.bandsOnlyCB.toggled.connect(self.apply_filters)
        self.deprecatedCB.toggled.connect(self.apply_filters)

        self.tagLE.returnPressed.connect(self.add_tag)
        self.tagClearBTN.clicked.connect(self.clear_tags)
        self.tagAnyRB.toggled.connect(self.apply_filters)

        self.timePresetCB.currentIndexChanged.connect(self.apply_time_preset)
        self.startDE.dateChanged.connect(self._on_start_changed)
        self.endDE.dateChanged.connect(self._on_dates_changed)
        self.timeFilterCB.toggled.connect(self.apply_filters)

        self.buildAoiBTN.clicked.connect(self.build_area_of_interest)
        self.useSelectionBTN.clicked.connect(self.use_selection_as_area)
        self.useCanvasBTN.clicked.connect(self.use_canvas_as_area)
        self.clearAoiBTN.clicked.connect(self.clear_area_of_interest)

        self.resultsView.selectionModel().selectionChanged.connect(
            self._on_selection_changed
        )
        self.resultsView.doubleClicked.connect(lambda _index: self.show_details())
        self.detailsBTN.clicked.connect(self.show_details)

        self.bandCB.currentIndexChanged.connect(self.update_estimate)
        self.resolutionLE.textChanged.connect(self.update_estimate)
        self.nativeResBTN.clicked.connect(self.use_native_resolution)
        self.extractBTN.clicked.connect(self.extract)
        self.styleBTN.clicked.connect(self.style_active_layer)

    # ------------------------------------------------------------------
    # Catalog loading and refreshing
    # ------------------------------------------------------------------

    def load_catalog(self) -> None:
        self.catalog = store.load_catalog()
        if not self.catalog:
            messages.push_warning(
                self.iface,
                self.tr(
                    "No catalog snapshot could be read. Use the refresh button "
                    "to download it from Google."
                ),
            )
        self._populate_filter_options()
        self.apply_filters()
        self._warn_if_stale()

    def _populate_filter_options(self) -> None:
        """Refill the type/provider combos, keeping the user's choice if possible."""
        for combo, values in (
            (self.typeCB, self.catalog.types()),
            (self.providerCB, self.catalog.providers()),
        ):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.tr("All"), None)
            for value in values:
                combo.addItem(value, value)
            if previous is not None:
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self._update_tag_completer()

    def _update_tag_completer(self, within=None) -> None:
        completer = QCompleter(self.catalog.tags(within), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.tagLE.setCompleter(completer)

    def _warn_if_stale(self) -> None:
        age = self.catalog.age_days()
        if age is not None and age > store.STALE_AFTER_DAYS:
            messages.push(
                self.iface,
                self.tr(
                    "The bundled catalog snapshot is {days} days old. Press "
                    "refresh to fetch the current one."
                ).format(days=age),
                Qgis.MessageLevel.Info,
                duration=8,
            )

    def refresh_catalog(self) -> None:
        task = CatalogRefreshTask()
        task.completed.connect(self._on_refresh_done)
        task.failed.connect(self._on_refresh_failed)
        task.progressDetail.connect(self.progressPanel.update_from)
        self._start_task(task)
        self.refreshBTN.setEnabled(False)
        self.progressPanel.start(
            self.tr("Refreshing catalog"), unit="dataset", indeterminate=True
        )

    def _on_refresh_done(self, catalog: Catalog, path: str) -> None:
        self.catalog = catalog
        self.refreshBTN.setEnabled(True)
        self.progressPanel.finish()
        self._populate_filter_options()
        self.apply_filters()
        messages.push_success(
            self.iface,
            self.tr("Catalog refreshed: {count} datasets.").format(count=len(catalog)),
        )
        messages.log(f"Catalog snapshot written to {path}")

    def _on_refresh_failed(self, message: str) -> None:
        self.refreshBTN.setEnabled(True)
        self.progressPanel.finish()
        messages.push_error(
            self.iface,
            self.tr("Could not refresh the catalog: {error}").format(error=message),
        )

    def cancel_current_task(self) -> None:
        """Ask the running job to stop.

        Cancellation is co-operative: a task checks between requests, so an
        Earth Engine call already in flight still has to come back.  The panel
        says so rather than appearing to hang.
        """
        live = (
            QgsTask.TaskStatus.Running,
            QgsTask.TaskStatus.Queued,
            QgsTask.TaskStatus.OnHold,
        )
        for task in list(self._tasks):
            try:
                if task.status() not in live:
                    continue
                task.cancel()
            except RuntimeError:
                # Qt destroyed the task between the signal and this call.
                self._forget_task(task)
                continue
            messages.push(
                self.iface,
                self.tr("Stopping after the requests already in flight…"),
                duration=4,
            )

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def current_filters(self) -> Filters:
        tags = tuple(self.tagList.item(row).text() for row in range(self.tagList.count()))
        # A preset's tags narrow the list the same way the user's own tags do, but
        # stay out of the tag list so Clear does not silently drop the preset.
        available = set(self.catalog.tags())
        tags += tuple(
            tag for tag in self.preset_filters().get("tags", ()) if tag in available
        )
        dataset_type = self.typeCB.currentData()
        provider = self.providerCB.currentData()
        start = end = None
        if self.timeFilterCB.isChecked():
            start = self.startDE.date().toString("yyyy-MM-dd")
            end = self.endDE.date().toString("yyyy-MM-dd")
        return Filters(
            text=self.searchLE.text().strip(),
            types=(dataset_type,) if dataset_type else (),
            providers=(provider,) if provider else (),
            tags=tags,
            tags_match_all=self.tagAllRB.isChecked(),
            start=start,
            end=end,
            bbox=tuple(self._aoi_bbox) if self._aoi_bbox else None,
            include_deprecated=self.deprecatedCB.isChecked(),
            bands_only=self.bandsOnlyCB.isChecked(),
        )

    def apply_filters(self) -> None:
        selected = self.selected_dataset()
        results = self.catalog.search(self.current_filters())
        self.resultsModel.set_datasets(results)
        self._update_results_label(len(results))
        self._update_tag_completer(results)
        if selected is not None:
            self._reselect(selected.id)

    def _update_results_label(self, count: int) -> None:
        total = len(self.catalog)
        if count == total:
            text = self.tr("{count} datasets").format(count=count)
        else:
            text = self.tr("{count} of {total} datasets").format(count=count, total=total)
        hidden = self.catalog.deprecated_count
        if hidden and not self.deprecatedCB.isChecked():
            text += self.tr(" ({hidden} deprecated hidden)").format(hidden=hidden)
        self.resultsLabel.setText(text)

    def _reselect(self, dataset_id: str) -> None:
        for row in range(self.resultsModel.rowCount()):
            dataset = self.resultsModel.dataset_at(row)
            if dataset is not None and dataset.id == dataset_id:
                index = self.resultsModel.index(row, 0)
                flags = QItemSelectionModel.SelectionFlag
                self.resultsView.selectionModel().select(
                    index, flags.ClearAndSelect | flags.Rows
                )
                self.resultsView.setCurrentIndex(index)
                return

    def preset_filters(self) -> dict:
        index = self.presetCB.currentIndex()
        return PRESETS[index][1] if 0 <= index < len(PRESETS) else {}

    def apply_preset(self) -> None:
        from qgis.PyQt.QtCore import QSettings

        QSettings().setValue(SETTINGS_PRESET, self.presetCB.currentIndex())
        self.apply_filters()

    def restore_preset(self) -> None:
        """Re-select the preset from last time, if it is still valid."""
        from qgis.PyQt.QtCore import QSettings

        try:
            index = int(QSettings().value(SETTINGS_PRESET, 0))
        except (TypeError, ValueError):
            index = 0
        if 0 <= index < self.presetCB.count():
            self.presetCB.blockSignals(True)
            self.presetCB.setCurrentIndex(index)
            self.presetCB.blockSignals(False)

    # -- tags -----------------------------------------------------------

    def add_tag(self) -> None:
        tag = self.tagLE.text().strip().lower()
        if not tag:
            return
        known = set(self.catalog.tags())
        if tag not in known:
            messages.push_warning(
                self.iface,
                self.tr("'{tag}' is not a tag used in the catalog.").format(tag=tag),
                duration=4,
            )
            return
        existing = {self.tagList.item(row).text() for row in range(self.tagList.count())}
        if tag not in existing:
            self.tagList.addItem(tag)
        self.tagLE.clear()
        self.apply_filters()

    def clear_tags(self) -> None:
        self.tagList.clear()
        self.tagLE.clear()
        self.apply_filters()

    def keyPressEvent(self, event) -> None:
        """Delete removes the selected tags when the tag list has focus."""
        if (
            event.key() == Qt.Key.Key_Delete
            and self.tagList.hasFocus()
            and self.tagList.selectedItems()
        ):
            for item in self.tagList.selectedItems():
                self.tagList.takeItem(self.tagList.row(item))
            self.apply_filters()
            return
        super().keyPressEvent(event)

    # -- time -----------------------------------------------------------

    def apply_time_preset(self) -> None:
        _, preset = TIME_PRESETS[self.timePresetCB.currentIndex()]
        today = QDate.currentDate()
        if preset is None:
            self.startDE.blockSignals(True)
            self.startDE.setDate(self.startDE.minimumDate())
            self.startDE.blockSignals(False)
            self.endDE.setDate(today)
            return

        kind, amount = preset
        if kind == "ytd":
            start = QDate(today.year(), 1, 1)
        elif kind == "months":
            start = today.addMonths(-amount)
        else:
            start = today.addDays(-amount)
        self.startDE.blockSignals(True)
        self.startDE.setDate(max(start, self.startDE.minimumDate()))
        self.startDE.blockSignals(False)
        self.endDE.setDate(today)

    def _on_start_changed(self) -> None:
        """Keep the window ordered without fighting the user's typing."""
        if self.startDE.date() > self.endDE.date():
            self.endDE.setDate(self.startDE.date())
        self._on_dates_changed()

    def _on_dates_changed(self) -> None:
        if self.timeFilterCB.isChecked():
            self.apply_filters()
        self.update_estimate()

    # ------------------------------------------------------------------
    # Area of interest
    # ------------------------------------------------------------------

    def _set_area(
        self, geojson: dict | None, bbox: list[float] | None, description: str
    ) -> None:
        self._aoi_geojson = geojson
        self._aoi_bbox = bbox
        self.aoiLabel.setText(description)
        self.apply_filters()
        self.update_estimate()

    def build_area_of_interest(self) -> None:
        try:
            longitude, latitude = geometry.validate_lonlat(
                self.lonLE.text(), self.latLE.text()
            )
            radius = float(self.radiusLE.text())
            sides = 4 if self.shapeCB.currentIndex() == 0 else 6
            ring = geometry.regular_polygon(
                longitude, latitude, radius, self.unitCB.currentText(), sides
            )
        except (geometry.GeometryError, ValueError) as error:
            messages.warn(
                self,
                self.tr("Cannot build that area"),
                str(error),
                self.tr("Enter a longitude, a latitude and a size."),
            )
            return

        try:
            layers.add_polygon(ring)
        except layers.LayerError as error:
            messages.push_warning(self.iface, str(error))

        bbox = geometry.bbox_of(ring)
        self._set_area(
            geometry.ring_to_geojson(ring),
            bbox,
            self.tr("Drawn area: {area:,.0f} km²").format(
                area=geometry.bbox_area_km2(bbox)
            ),
        )
        layers.zoom_to(self.iface, bbox)

    def use_selection_as_area(self) -> None:
        try:
            geojson = layers.selected_geometry_as_geojson(self.iface)
        except layers.LayerError as error:
            messages.push_warning(self.iface, str(error))
            return
        if geojson is None:
            messages.warn(
                self,
                self.tr("Nothing selected"),
                self.tr("No features are selected on any vector layer."),
                self.tr(
                    "Select features with the Select tool, then press Use "
                    "selection again. The layer does not have to be the active "
                    "one. To draw an area instead, enter a longitude, latitude "
                    "and size above and press Draw area."
                ),
            )
            return
        bbox = layers.geojson_bbox(geojson)
        # Name the source layers: the selection is often on a different layer from
        # the one highlighted in the Layers panel, and saying which was used is how
        # the user knows the area is the one they meant.
        self._set_area(
            geojson,
            bbox,
            self.tr("Selection ({source}): {area:,.0f} km²").format(
                source=layers.describe_selection(self.iface),
                area=geometry.bbox_area_km2(bbox) if bbox else 0,
            ),
        )

    def use_canvas_as_area(self) -> None:
        try:
            bbox = layers.canvas_extent_bbox(self.iface)
        except layers.LayerError as error:
            messages.push_warning(self.iface, str(error))
            return
        if not bbox:
            messages.push_warning(self.iface, self.tr("The map canvas is empty."))
            return
        ring = geometry.bbox_polygon(bbox)
        self._set_area(
            geometry.ring_to_geojson(ring),
            bbox,
            self.tr("Canvas extent: {area:,.0f} km²").format(
                area=geometry.bbox_area_km2(bbox)
            ),
        )

    def clear_area_of_interest(self) -> None:
        layers.remove_layers_named("Area of interest")
        self._set_area(None, None, self.tr("No area of interest set"))

    # ------------------------------------------------------------------
    # Selection and details
    # ------------------------------------------------------------------

    def selected_dataset(self) -> Dataset | None:
        index = self.resultsView.currentIndex()
        if not index.isValid():
            return None
        return index.data(DATASET_ROLE)

    def _on_selection_changed(self, *_args) -> None:
        dataset = self.selected_dataset()
        self.detailsBTN.setEnabled(dataset is not None)
        self._populate_bands(dataset)
        self.update_estimate()

    def _populate_bands(self, dataset: Dataset | None) -> None:
        self.bandCB.blockSignals(True)
        self.bandCB.clear()
        if dataset is not None and dataset.bands:
            if len(dataset.bands) > 1:
                self.bandCB.addItem(ALL_BANDS, None)
            for band in dataset.bands:
                self.bandCB.addItem(band.label, band.name)
        self.bandCB.blockSignals(False)
        self.bandCB.setEnabled(self.bandCB.count() > 0)

        if dataset is not None and not self.resolutionLE.text():
            self.use_native_resolution()

    def use_native_resolution(self) -> None:
        dataset = self.selected_dataset()
        if dataset is None:
            return
        resolution = dataset.default_resolution()
        if resolution:
            self.resolutionLE.setText(f"{resolution:g}")
        else:
            messages.push(
                self.iface,
                self.tr("Google does not publish a resolution for that dataset."),
                duration=4,
            )

    def show_details(self) -> None:
        dataset = self.selected_dataset()
        if dataset is None:
            return
        messages.long_text(
            self, self.tr("Dataset details"), details.dataset_html(dataset)
        )

    # ------------------------------------------------------------------
    # Estimating and extracting
    # ------------------------------------------------------------------

    def selected_bands(self) -> tuple[str, ...]:
        dataset = self.selected_dataset()
        if dataset is None:
            return ()
        chosen = self.bandCB.currentData()
        if chosen is None:
            return dataset.band_names
        return (chosen,)

    def build_request(self) -> ExtractRequest:
        """Assemble a request from the current selections.

        Raises :class:`RequestError` when something is still missing; both the
        live estimate and :meth:`extract` go through here so they can never
        disagree about what would be sent.
        """
        dataset = self.selected_dataset()
        if dataset is None:
            raise RequestError(self.tr("Select a dataset first"))

        try:
            resolution = float(self.resolutionLE.text() or 0)
        except (TypeError, ValueError):
            resolution = 0.0

        bbox = self._aoi_bbox or (list(dataset.bbox) if dataset.bbox else None)
        start = self.startDE.date().toString("yyyy-MM-dd")
        end = self.endDE.date().toString("yyyy-MM-dd")
        # Clamp the window to what the dataset actually covers.  The default
        # "All available" preset winds the start back to 1970, and without this
        # the image-count estimate counts decades that hold no data -- which made
        # every estimate look wildly over the limit.
        dataset_start = dates.to_ee_date(dataset.start)
        if dataset_start and dataset_start > start:
            start = dataset_start
        dataset_end = dates.to_ee_date(dataset.effective_end)
        if dataset_end and dataset_end < end:
            end = dataset_end
        if start > end:
            # The dataset ended before the window the user is looking at.
            start = end

        return ExtractRequest(
            dataset_id=dataset.id,
            dataset_type=dataset.type,
            bands=self.selected_bands(),
            start=start,
            end=end,
            resolution_m=resolution,
            bbox=tuple(bbox) if bbox else None,
            interval=dataset.interval,
        )

    def update_estimate(self) -> None:
        dataset = self.selected_dataset()
        if dataset is None:
            self.estimateLabel.setText("")
            self.extractBTN.setEnabled(False)
            self.styleBTN.setEnabled(False)
            return

        self.styleBTN.setEnabled(bool(dataset.visualizations))

        try:
            request = self.build_request()
        except (RequestError, ValueError) as error:
            self.estimateLabel.setText(f"<i>{error}</i>")
            self.extractBTN.setEnabled(False)
            return

        self.extractBTN.setEnabled(True)
        estimate = request.estimate()
        if estimate.values == 0:
            self.estimateLabel.setText(
                self.tr("Size cannot be estimated for this dataset.")
            )
            return

        if estimate.fits and not estimate.should_warn:
            self.estimateLabel.setText(
                self.tr("Estimated {values:,} values — within the limit.").format(
                    values=estimate.values
                )
            )
            return

        advice = []
        suggested = estimate.suggested_resolution(request.resolution_m)
        if suggested:
            advice.append(self.tr("try about {res}").format(res=format_metres(suggested)))
        days = estimate.suggested_days(request.days)
        if days:
            advice.append(self.tr("or about {days}").format(days=format_days(days)))
        if not self._aoi_bbox:
            advice.append(self.tr("or set an area of interest"))

        colour = "#c0392b" if not estimate.fits else "#b9770e"
        headline = (
            self.tr("Estimated {values:,} values — over the {limit:,} limit.")
            if not estimate.fits
            else self.tr("Estimated {values:,} values — close to the {limit:,} limit.")
        ).format(values=estimate.values, limit=estimate.limit)
        self.estimateLabel.setText(
            f"<span style='color:{colour}'>{headline}</span>"
            + (f"<br>{'; '.join(advice)}." if advice else "")
        )

    def extract(self) -> None:
        dataset = self.selected_dataset()
        if dataset is None:
            return

        if not earthengine.is_available():
            messages.error(
                self,
                self.tr("Earth Engine is not available"),
                self.tr("The Earth Engine Python API could not be imported."),
                self.tr(
                    "Install the 'Google Earth Engine' plugin, which provides it, "
                    "then restart QGIS."
                ),
            )
            return

        # Importable is not the same as signed in. The Earth Engine plugin bundles
        # its own copy of `ee`, which imports fine even when that plugin failed to
        # initialise -- and then the first call raises deep inside ee. Checking
        # here turns that into one clear message.
        if not earthengine.is_initialised():
            messages.error(
                self,
                self.tr("Earth Engine is not signed in"),
                self.tr("No Earth Engine session is available."),
                earthengine.NOT_INITIALISED_ADVICE,
            )
            return

        try:
            request = self.build_request()
        except (RequestError, ValueError) as error:
            messages.warn(self, self.tr("Request not ready"), str(error))
            return

        # Offer to tile the job rather than just refusing it.  Auto-scaling is the
        # useful answer here: "too large" nearly always means "too large for one
        # request", not "impossible".
        estimate = request.estimate()
        too_large = not estimate.fits and estimate.values
        if too_large and self._offer_autoscale(dataset, request, estimate):
            return

        # An area of interest is required; the dataset footprint is often global,
        # and a global request at native resolution cannot succeed.
        if (
            self._aoi_geojson is None
            and self._aoi_bbox is None
            and not messages.confirm(
                self,
                self.tr("No area of interest is set"),
                self.tr("Download the whole of {dataset}?").format(dataset=dataset.id),
                self._no_area_warning(dataset),
            )
        ):
            return

        try:
            request.region = self._build_region(request)
        except earthengine.EarthEngineNotInitialised as error:
            messages.error(self, self.tr("Earth Engine is not signed in"), str(error))
            return
        except earthengine.EarthEngineError as error:
            messages.error(self, self.tr("Cannot build the area of interest"), str(error))
            return

        task = ExtractTask(request)
        task.completed.connect(self._on_extract_done)
        task.failed.connect(self._on_extract_failed)
        self._start_task(task)
        self.extractBTN.setEnabled(False)
        # One opaque call: Earth Engine reports no progress, so show a busy bar
        # with a ticking elapsed time rather than a percentage that never moves.
        self.progressPanel.start(
            self.tr("Extracting {dataset}").format(dataset=dataset.id),
            indeterminate=True,
        )
        messages.push(
            self.iface,
            self.tr("Extracting {dataset}…").format(dataset=dataset.id),
            duration=3,
        )

    def _no_area_warning(self, dataset) -> str:
        """Say what extracting with no area actually means for this dataset.

        Tables get no size estimate -- feature density is not published -- so
        without this the only signal is "Size cannot be estimated", and a request
        for every county in the United States looks the same as a small one.
        """
        extent = (
            self.tr("the whole world")
            if dataset.is_global
            else self.tr("its entire published extent")
        )
        lines = [
            self.tr(
                "Nothing on the Area tab is set, so this would ask Earth Engine "
                "for {extent}."
            ).format(extent=extent),
        ]
        if dataset.type == "FeatureCollection":
            lines.append(
                self.tr(
                    "That means every feature in the dataset. Earth Engine stops "
                    "at {limit:,} features per request, so a large table will come "
                    "back truncated with no warning."
                ).format(limit=plan_mod.FEATURE_LIMIT)
            )
        else:
            lines.append(
                self.tr("That is very likely to exceed the Earth Engine request limit.")
            )
        lines.append(
            self.tr(
                "Set an area first: select features on any layer and press Use "
                "selection, draw one, or press Use canvas."
            )
        )
        return "\n\n".join(lines)

    def _build_region(self, request: ExtractRequest):
        if self._aoi_geojson is not None:
            return earthengine.geojson_to_ee(self._aoi_geojson)
        if request.bbox:
            return earthengine.bbox_to_ee(list(request.bbox))
        raise earthengine.EarthEngineError("No area of interest is set")

    # ------------------------------------------------------------------
    # Auto-scaling a too-large request
    # ------------------------------------------------------------------

    def _area_geojson(self, request: ExtractRequest) -> dict | None:
        """The area to tile: the user's own shape if set, else the bounding box."""
        if self._aoi_geojson is not None:
            return self._aoi_geojson
        if request.bbox:
            return geometry.ring_to_geojson(geometry.bbox_polygon(list(request.bbox)))
        return None

    def _offer_autoscale(self, dataset, request: ExtractRequest, estimate) -> bool:
        """Ask whether to tile the job. Returns True if it was handled here."""
        suggested = estimate.suggested_resolution(request.resolution_m)
        detail = estimate.describe()
        if suggested:
            detail += "\n\n" + self.tr(
                "Alternatively a ground resolution of about {res} would fit in "
                "one request."
            ).format(res=format_metres(suggested))

        if not messages.confirm(
            self,
            self.tr("This request is too large for one call"),
            self.tr("Split the area into tiles and fetch them one after another?"),
            detail,
            default_yes=True,
        ):
            # They declined tiling: let them send it as-is and see what happens.
            return not messages.confirm(
                self,
                self.tr("Send it anyway?"),
                self.tr("Earth Engine will probably refuse this request."),
                detail,
            )

        area = self._area_geojson(request)
        if area is None:
            messages.warn(
                self,
                self.tr("No area to tile"),
                self.tr("Set an area of interest on the Area tab first."),
            )
            return True

        grid = hexgrid.grid()

        def build(resolution: int | None = None):
            return plan_mod.plan(
                dataset,
                area,
                request.bands,
                request.start,
                request.end,
                request.resolution_m,
                tile_grid=grid,
                force_tiling=True,
                minimum_resolution=(
                    resolution
                    if resolution is not None
                    else hexgrid.DEFAULT_MIN_RESOLUTION
                ),
                maximum_resolution=(
                    resolution
                    if resolution is not None
                    else hexgrid.DEFAULT_MAX_RESOLUTION
                ),
            )

        try:
            extraction_plan = build()
        except plan_mod.PlanError as error:
            messages.warn(self, self.tr("Cannot split this request"), str(error))
            return True

        dialog = AutoScaleDialog(extraction_plan, replan=build, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return True

        self._run_chunked(dialog.plan, dialog.workers, grid)
        return True

    def _run_chunked(self, extraction_plan, workers: int, grid) -> None:
        task = ChunkedExtractTask(extraction_plan, workers=workers, tile_grid=grid)
        task.completed.connect(self._on_chunked_done)
        task.failed.connect(self._on_chunked_failed)
        task.note.connect(lambda text: messages.push(self.iface, text, duration=6))
        task.progressDetail.connect(self.progressPanel.update_from)
        self._chunked_plan = extraction_plan
        self._start_task(task)

        self.extractBTN.setEnabled(False)
        self.progressPanel.start(
            self.tr("Extracting"),
            total=extraction_plan.tile_count,
            unit="tile",
            expected_rate=progress_mod.rate_from_estimate(
                extraction_plan.tile_count, extraction_plan.seconds(workers)
            ),
        )
        messages.push(
            self.iface,
            self.tr(
                "Extracting {count} tiles from {dataset} with {how}. Estimated {eta}."
            ).format(
                count=extraction_plan.tile_count,
                dataset=extraction_plan.dataset_id,
                how=concurrency.describe_workers(workers),
                eta=concurrency.format_duration(extraction_plan.seconds(workers)),
            ),
            duration=8,
        )

    def _on_chunked_done(self, data, summary: str) -> None:
        self._reset_progress()
        plan_used = getattr(self, "_chunked_plan", None)
        self._last_result = data
        self._last_request = ExtractRequest(
            dataset_id=plan_used.dataset_id,
            dataset_type=plan_used.dataset_type,
            bands=plan_used.bands,
            start=plan_used.start,
            end=plan_used.end,
            resolution_m=plan_used.resolution_m,
            bbox=tuple(self._aoi_bbox) if self._aoi_bbox else None,
        )
        messages.push_success(self.iface, summary, duration=10)
        messages.log(f"Chunked extraction finished: {summary}")
        self.save_last_result()

    def _on_chunked_failed(self, what: str, advice: str) -> None:
        self._reset_progress()
        messages.error(self, self.tr("Chunked extraction failed"), what, advice)

    def _reset_progress(self) -> None:
        self.progressPanel.finish()
        self.extractBTN.setEnabled(True)

    def _on_extract_done(self, request: ExtractRequest, data) -> None:
        self._reset_progress()
        count = (
            len(data)
            if isinstance(data, list)
            else len((data or {}).get("features") or ())
        )
        if not count:
            messages.warn(
                self,
                self.tr("No data returned"),
                self.tr("Earth Engine accepted the request but returned nothing."),
                self.tr(
                    "The filters probably select no images. Widen the dates or "
                    "move the area of interest, then try again."
                ),
            )
            return

        self._last_result = data
        self._last_request = request
        messages.push_success(
            self.iface,
            self.tr("{count:,} records returned from {dataset}.").format(
                count=count, dataset=request.dataset_id
            ),
        )
        self.save_last_result()

    def _on_extract_failed(self, request: ExtractRequest, what: str, advice: str) -> None:
        self._reset_progress()
        messages.error(self, self.tr("Earth Engine rejected the request"), what, advice)

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def save_last_result(self) -> None:
        if self._last_result is None or self._last_request is None:
            return
        request = self._last_request
        suggested = "{}_{}".format(
            request.dataset_id.replace("/", "_"),
            (request.start or "").replace("-", ""),
        )
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            self.tr("Save extracted data"),
            suggested,
            export.dialog_filter(),
        )
        if not path:
            messages.push(
                self.iface,
                self.tr("Extraction kept in memory. Use Extract again to re-save."),
                duration=5,
            )
            return

        extension = export.extension_from_filter(selected_filter)
        try:
            written = export.save(
                self._last_result,
                path,
                extension=extension,
                resolution_m=request.resolution_m,
            )
        except export.ExportError as error:
            messages.error(self, self.tr("Could not save"), str(error))
            return
        except OSError as error:
            messages.error(self, self.tr("Could not write the file"), str(error))
            return

        messages.push_success(
            self.iface,
            self.tr("Saved {name}").format(name=os.path.basename(written)),
        )
        self._last_result = None
        self._add_result_layer(written)

    def _add_result_layer(self, path: str) -> None:
        if not path.lower().endswith(".geojson"):
            # CSV and JSON are not loadable as vector layers without a geometry
            # definition; saying so beats the original's silent failure.
            messages.push(
                self.iface,
                self.tr(
                    "Saved as {ext}. Save as GeoJSON to add it to the map automatically."
                ).format(ext=os.path.splitext(path)[1]),
                duration=6,
            )
            return
        try:
            layer = layers.add_vector_file(path)
        except layers.LayerError as error:
            messages.push_warning(self.iface, str(error))
            return
        if self._aoi_bbox:
            layers.zoom_to(self.iface, self._aoi_bbox)
        messages.push_success(
            self.iface, self.tr("Added layer '{name}'").format(name=layer.name())
        )

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    def style_active_layer(self) -> None:
        dataset = self.selected_dataset()
        if dataset is None:
            return
        bands = self.selected_bands()
        band_name = bands[0] if bands else None
        visualisation = (dataset.visualisation_for(band_name) if band_name else None) or (
            dataset.visualizations[0] if dataset.visualizations else None
        )
        if visualisation is None or not visualisation.palette:
            messages.push_warning(
                self.iface,
                self.tr("That dataset has no colour palette to apply."),
            )
            return

        field = band_name or (visualisation.bands[0] if visualisation.bands else None)
        if not field:
            messages.push_warning(self.iface, self.tr("Select a band first."))
            return

        layer = self.iface.activeLayer() if self.iface else None
        try:
            classes = layers.apply_graduated_style(layer, field, visualisation.palette)
        except layers.LayerError as error:
            messages.warn(self, self.tr("Could not style the layer"), str(error))
            return
        messages.push_success(
            self.iface,
            self.tr("Styled '{field}' with {classes} classes.").format(
                field=field, classes=classes
            ),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _start_task(self, task) -> None:
        """Hand a task to QGIS, keeping a reference so it is not collected.

        The reference is dropped when the task reports that it has ended, rather
        than by asking sip whether the C++ object is still alive: ``qgis.PyQt.sip``
        is not available on every build, and a list of possibly-dead objects is a
        RuntimeError waiting to happen the next time anything iterates it.
        """
        self._tasks.append(task)
        task.taskCompleted.connect(lambda t=task: self._forget_task(t))
        task.taskTerminated.connect(lambda t=task: self._forget_task(t))
        QgsApplication.taskManager().addTask(task)

    def _forget_task(self, task) -> None:
        # Both taskCompleted and taskTerminated may fire, so a second removal is
        # expected rather than exceptional.
        with contextlib.suppress(ValueError):
            self._tasks.remove(task)

    def closeEvent(self, event) -> None:
        self._search_timer.stop()
        self.closingPlugin.emit()
        event.accept()
