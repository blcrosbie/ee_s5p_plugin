"""The auto-scale dialog: show the tiling plan, let the user set concurrency.

Offered when a request is too large for Earth Engine to answer in one go.  Rather
than just refusing, the plugin works out a tiling that *would* fit and shows what
it would cost, so the decision is informed: how many tiles, how big each one is,
how long it will take sequentially, and how much faster each worker count is.

The resolution can be overridden. Auto-scaling picks the coarsest tiling that
fits, which is the fewest requests; a user who is being rate limited may prefer
finer tiles, and one extracting a single small area may want the opposite.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from ..core import concurrency, hexgrid
from ..core import plan as plan_mod

#: Where the chosen worker count is remembered between sessions.
SETTINGS_WORKERS = "ee_s5p_plugin/workers"
SETTINGS_AUTOSCALE = "ee_s5p_plugin/autoscale_enabled"


def remembered_workers(default: int | None = None) -> int:
    """The worker count from last time, clamped to what this machine allows."""
    from qgis.PyQt.QtCore import QSettings

    default = default if default is not None else concurrency.default_workers()
    try:
        stored = int(QSettings().value(SETTINGS_WORKERS, default))
    except (TypeError, ValueError):
        stored = default
    return max(1, min(stored, concurrency.max_workers()))


def remember_workers(workers: int) -> None:
    from qgis.PyQt.QtCore import QSettings

    QSettings().setValue(SETTINGS_WORKERS, int(workers))


class AutoScaleDialog(QDialog):
    """Review and adjust a chunked extraction before it runs."""

    def __init__(self, extraction_plan: plan_mod.Plan, replan=None, parent=None):
        """``replan(resolution)`` returns a new plan at that tile resolution."""
        super().__init__(parent)
        self.setWindowTitle(self.tr("Auto-scale this extraction"))
        self.setMinimumSize(520, 520)

        self.plan = extraction_plan
        self._replan = replan
        self._auto_resolution = extraction_plan.tile_resolution

        layout = QVBoxLayout(self)

        intro = QLabel(
            self.tr(
                "This request is too large for Earth Engine to answer at once. "
                "It can be split into tiles and fetched piece by piece."
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()

        self.resolutionCB = QComboBox()
        self.resolutionCB.setToolTip(
            self.tr(
                "Coarser tiles mean fewer requests. Finer tiles are smaller and "
                "less likely to be refused."
            )
        )
        form.addRow(self.tr("Tile size"), self.resolutionCB)

        self.workersCB = QComboBox()
        self.workersCB.setToolTip(
            self.tr(
                "Requests are network-bound, so running several at once is much "
                "faster. Capped at half this machine's CPUs. Sequential never "
                "trips Earth Engine's rate limit."
            )
        )
        form.addRow(self.tr("Concurrency"), self.workersCB)
        layout.addLayout(form)

        self.gridLabel = QLabel()
        self.gridLabel.setWordWrap(True)
        layout.addWidget(self.gridLabel)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(True)
        layout.addWidget(self.summary, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Run"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._populate_resolutions()
        self._populate_workers()
        self._describe_grid()
        self._refresh()

        self.resolutionCB.currentIndexChanged.connect(self._on_resolution_changed)
        self.workersCB.currentIndexChanged.connect(self._refresh)

    # -- population -------------------------------------------------------

    def _populate_resolutions(self) -> None:
        self.resolutionCB.blockSignals(True)
        self.resolutionCB.clear()
        current = self.plan.tile_resolution
        lowest = max(hexgrid.COARSEST_RESOLUTION, (current or 5) - 2)
        highest = min(hexgrid.FINEST_RESOLUTION, (current or 5) + 3)
        for resolution in range(lowest, highest + 1):
            label = hexgrid.describe_resolution(resolution)
            if resolution == self._auto_resolution:
                label += self.tr("  [auto]")
            self.resolutionCB.addItem(label, resolution)
        index = self.resolutionCB.findData(current)
        if index >= 0:
            self.resolutionCB.setCurrentIndex(index)
        self.resolutionCB.blockSignals(False)

    def _populate_workers(self) -> None:
        self.workersCB.blockSignals(True)
        self.workersCB.clear()
        preferred = remembered_workers()
        for workers in concurrency.worker_choices():
            seconds = self.plan.seconds(workers)
            how = concurrency.describe_workers(workers)
            eta = concurrency.format_duration(seconds)
            label = f"{how} — {eta}"
            if workers > 1:
                label += f" ({self.plan.speedup(workers):.1f}×)"
            self.workersCB.addItem(label, workers)
        index = self.workersCB.findData(preferred)
        self.workersCB.setCurrentIndex(max(0, index))
        self.workersCB.blockSignals(False)

    def _describe_grid(self) -> None:
        if self.plan.indexed:
            self.gridLabel.setText(
                self.tr(
                    "Tiles are H3 cells ({grid}). Every extracted row is "
                    "stamped with its <code>h3_index</code>, so the output can be "
                    "joined against anything else keyed by H3."
                ).format(grid=self.plan.grid_name)
            )
            return
        self.gridLabel.setTextFormat(Qt.TextFormat.RichText)
        self.gridLabel.setText(
            self.tr(
                "<b>h3 is not installed</b>, so tiles are a plain "
                "latitude/longitude grid. They still cover the area exactly, but "
                "the ids are not real H3 indexes.<br><br>To get H3 indexes:<br>"
                "<code>{hint}</code>"
            ).format(hint=hexgrid.install_hint().replace("\n", "<br>"))
        )

    # -- interaction ------------------------------------------------------

    def _on_resolution_changed(self) -> None:
        resolution = self.resolutionCB.currentData()
        if self._replan is None or resolution is None:
            self._refresh()
            return
        try:
            self.plan = self._replan(int(resolution))
        except plan_mod.PlanError as error:
            self.summary.setPlainText(
                self.tr("That tile size will not work:\n\n{error}").format(error=error)
            )
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self._populate_workers()
        self._refresh()

    def _refresh(self) -> None:
        workers = self.workers
        text = self.plan.describe(workers=workers)
        html = "<pre style='white-space:pre-wrap'>{}</pre>".format(
            text.replace("&", "&amp;").replace("<", "&lt;")
        )
        if self.plan.tile_count > 500:
            html += self.tr(
                "<p><b>{count:,} requests.</b> Earth Engine may start rate "
                "limiting; the plugin backs off and retries automatically, so the "
                "job will slow down rather than fail.</p>"
            ).format(count=self.plan.tile_count)
        self.summary.setHtml(html)

    # -- results ----------------------------------------------------------

    @property
    def workers(self) -> int:
        value = self.workersCB.currentData()
        return int(value) if value else 1

    def accept(self) -> None:
        remember_workers(self.workers)
        super().accept()
