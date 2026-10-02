"""The status panel: a progress bar, a live status line, and Cancel.

Built from plain Qt widgets that the plugin already imports -- no extra
dependency, which matters for something shipped through the QGIS plugin
repository.

Two shapes of job use it:

*determinate*
    A catalog refresh, or a tiled extraction, where the number of pieces is known.
    Shows a real percentage, the piece count, records so far, throughput and a
    remaining-time estimate that corrects itself from measured throughput.

*indeterminate*
    One opaque Earth Engine call, where there is no progress to report at all.
    Shows a busy bar and a ticking elapsed time, so a heavy feature collection
    still looks alive rather than hung.

Progress arrives from worker threads as a plain dict, which crosses Qt's queued
connection safely; the widget is only ever touched on the GUI thread.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
)

from ..core.progress import ProgressSnapshot

#: How often the elapsed time ticks while a job is indeterminate.
TICK_MS = 500


class ProgressPanel(QFrame):
    """Shows what a long running job is doing, and offers to stop it."""

    #: Emitted when the user presses Cancel.
    cancelRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVisible(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 0)
        outer.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(4)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top.addWidget(self.bar, stretch=1)

        self.cancelBTN = QToolButton()
        self.cancelBTN.setText("✕")
        self.cancelBTN.setToolTip(self.tr("Stop this job"))
        self.cancelBTN.setAutoRaise(True)
        self.cancelBTN.clicked.connect(self._on_cancel)
        top.addWidget(self.cancelBTN)
        outer.addLayout(top)

        self.statusLabel = QLabel()
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setTextFormat(Qt.TextFormat.PlainText)
        # Keep the dock from jumping as the text grows and shrinks.
        self.statusLabel.setMinimumHeight(self.statusLabel.fontMetrics().height() * 2)
        self.statusLabel.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        outer.addWidget(self.statusLabel)

        self._ticker = QTimer(self)
        self._ticker.setInterval(TICK_MS)
        self._ticker.timeout.connect(self._tick)
        self._snapshot: ProgressSnapshot | None = None
        self._cancelling = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        phase: str,
        total: int = 0,
        unit: str = "tile",
        indeterminate: bool = False,
        cancellable: bool = True,
        expected_rate: float = 0.0,
    ) -> None:
        """Show the panel for a job that is beginning."""
        self._cancelling = False
        self.cancelBTN.setEnabled(cancellable)
        self.cancelBTN.setVisible(cancellable)
        self.cancelBTN.setText("✕")

        snapshot = ProgressSnapshot(
            phase=phase,
            total=total,
            unit=unit,
            indeterminate=indeterminate or total <= 0,
            expected_rate=expected_rate,
        )
        self._apply(snapshot)
        self.setVisible(True)
        if snapshot.indeterminate:
            self._ticker.start()

    def update_from(self, payload: dict | ProgressSnapshot) -> None:
        """Take a snapshot from a running job, however it was delivered."""
        snapshot = (
            payload
            if isinstance(payload, ProgressSnapshot)
            else ProgressSnapshot.from_dict(payload)
        )
        self._apply(snapshot)
        if snapshot.indeterminate and not self._ticker.isActive():
            self._ticker.start()
        elif not snapshot.indeterminate and self._ticker.isActive():
            self._ticker.stop()

    def set_percent(self, percent: float, phase: str | None = None) -> None:
        """Simple determinate update, for a job with no richer statistics."""
        base = self._snapshot or ProgressSnapshot()
        self._apply(
            ProgressSnapshot(
                phase=phase if phase is not None else base.phase,
                done=int(max(0.0, min(100.0, percent))),
                total=100,
                unit=base.unit,
                elapsed=base.elapsed,
                records=base.records,
            )
        )

    def finish(self, message: str = "") -> None:
        """Hide the panel, leaving the final message in the status line."""
        self._ticker.stop()
        if message:
            self.statusLabel.setText(message)
        self.setVisible(False)
        self._snapshot = None
        self._cancelling = False

    def is_busy(self) -> bool:
        return self.isVisible()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply(self, snapshot: ProgressSnapshot) -> None:
        self._snapshot = snapshot

        if snapshot.indeterminate:
            # A 0..0 range is Qt's busy indicator.
            if self.bar.maximum() != 0:
                self.bar.setRange(0, 0)
        else:
            if self.bar.maximum() != 100:
                self.bar.setRange(0, 100)
            self.bar.setValue(snapshot.percent or 0)

        self.bar.setFormat(snapshot.bar_format())
        text = snapshot.status_line()
        if self._cancelling:
            text = self.tr("Stopping… ") + text
        self.statusLabel.setText(text)

    def _tick(self) -> None:
        """Keep the elapsed time moving while a job reports nothing."""
        if self._snapshot is None:
            self._ticker.stop()
            return
        current = self._snapshot
        self._apply(
            ProgressSnapshot(
                phase=current.phase,
                done=current.done,
                total=current.total,
                records=current.records,
                failures=current.failures,
                throttles=current.throttles,
                elapsed=current.elapsed + TICK_MS / 1000.0,
                rate=current.rate,
                expected_rate=current.expected_rate,
                unit=current.unit,
                indeterminate=current.indeterminate,
                throttled=current.throttled,
                note=current.note,
            )
        )

    def _on_cancel(self) -> None:
        # Cancellation is co-operative: the task checks between requests, so say
        # it has been asked for rather than pretending it is already done.
        self._cancelling = True
        self.cancelBTN.setEnabled(False)
        if self._snapshot is not None:
            self._apply(self._snapshot)
        self.cancelRequested.emit()
