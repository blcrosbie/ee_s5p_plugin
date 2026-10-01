"""Background work, so the QGIS window never freezes.

Every network call in v0.1 ran on the GUI thread: refreshing the catalog, reading
dataset details, and extracting data all locked QGIS up, sometimes for minutes,
with no progress and no way to cancel.  They are :class:`QgsTask` subclasses here,
which gives progress reporting, cancellation and the QGIS task manager's own UI.

``run()`` executes on a worker thread, so it must not touch widgets.  Results are
stashed on the task and read back in :meth:`finished`, which Qt invokes on the
main thread.
"""

from __future__ import annotations

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ..core import earthengine, stac, store
from ..core import plan as plan_mod
from ..core.catalog import Catalog
from ..core.request import ExtractRequest


class CatalogRefreshTask(QgsTask):
    """Crawl the Earth Engine STAC catalog and save a new snapshot."""

    #: ``(catalog, path)`` on success.
    completed = pyqtSignal(object, str)
    #: ``(message,)`` on failure or cancellation.
    failed = pyqtSignal(str)

    def __init__(self, description: str = "Refreshing the Earth Engine catalog"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._catalog: Catalog | None = None
        self._path: str = ""
        self._error: str = ""

    def run(self) -> bool:
        def progress(done: int, total: int, message: str) -> bool:
            if self.isCanceled():
                return False
            if total:
                self.setProgress(100.0 * done / total)
            return True

        try:
            self._catalog, self._path = store.refresh(progress=progress)
        except stac.StacError as error:
            self._error = str(error)
            return False
        except Exception as error:
            self._error = f"Unexpected failure refreshing the catalog: {error}"
            return False

        if self.isCanceled():
            self._error = "Refresh cancelled"
            return False
        return True

    def finished(self, result: bool) -> None:
        if result and self._catalog is not None:
            self.completed.emit(self._catalog, self._path)
        else:
            self.failed.emit(self._error or "Refresh cancelled")


class DescribeTask(QgsTask):
    """Fetch a dataset's server-side description for the details panel."""

    completed = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, dataset_id: str, dataset_type: str):
        super().__init__(f"Reading {dataset_id}", QgsTask.Flag.CanCancel)
        self.dataset_id = dataset_id
        self.dataset_type = dataset_type
        self._info: object = None
        self._error: str = ""

    def run(self) -> bool:
        try:
            self._info = earthengine.describe(self.dataset_id, self.dataset_type)
        except (
            earthengine.EarthEngineError,
            earthengine.EarthEngineUnavailable,
        ) as error:
            self._error = str(error)
            return False
        except Exception as error:
            self._error = str(error)
            return False
        return not self.isCanceled()

    def finished(self, result: bool) -> None:
        if result:
            self.completed.emit(self.dataset_id, self._info)
        else:
            self.failed.emit(self.dataset_id, self._error or "Cancelled")


class ExtractTask(QgsTask):
    """Run one extraction against Earth Engine."""

    #: ``(request, data)`` -- ``data`` is a list of rows or a GeoJSON mapping.
    completed = pyqtSignal(object, object)
    #: ``(request, what_happened, what_to_try)``
    failed = pyqtSignal(object, str, str)

    def __init__(self, request: ExtractRequest):
        super().__init__(f"Extracting {request.dataset_id}", QgsTask.Flag.CanCancel)
        self.request = request
        self._data: object = None
        self._error: tuple[str, str] | None = None

    def run(self) -> bool:
        from ..core.request import explain_error, parse_limit_error

        try:
            self._data = earthengine.extract(self.request)
        except Exception as error:
            what, advice = explain_error(error)
            # When Earth Engine reports the real numbers, use them: they beat our
            # local approximation for telling the user what would actually fit.
            actual = parse_limit_error(error)
            if actual is not None:
                suggested = actual.suggested_resolution(self.request.resolution_m)
                advice = f"{actual.describe()}\n\n{advice}"
                if suggested:
                    advice += f"\n\nTry a resolution of about {suggested:g} m."
            self._error = (what, advice)
            return False

        if self.isCanceled():
            self._error = ("Extraction cancelled", "")
            return False
        return True

    @property
    def row_count(self) -> int:
        if isinstance(self._data, list):
            return len(self._data)
        if isinstance(self._data, dict):
            return len(self._data.get("features") or ())
        return 0

    def finished(self, result: bool) -> None:
        if result:
            self.completed.emit(self.request, self._data)
        else:
            what, advice = self._error or ("Extraction failed", "")
            self.failed.emit(self.request, what, advice)


class ChunkedExtractTask(QgsTask):
    """Run a tiled extraction, optionally several requests at a time.

    The whole job is one :class:`QgsTask`, so QGIS shows a single cancellable
    entry; the parallelism happens inside :meth:`run` via
    :func:`~...core.concurrency.run_jobs`.  Tiles that Earth Engine refuses as too
    large are subdivided and retried, which is what lets the plan start from
    arithmetic instead of from a probe.
    """

    #: ``(rows_or_feature_collection, summary)`` once everything has settled.
    completed = pyqtSignal(object, str)
    #: ``(what_happened, what_to_try)``
    failed = pyqtSignal(str, str)
    #: ``(message,)`` for the status line while the job runs.
    note = pyqtSignal(str)

    #: How many times a refused tile may be subdivided before giving up.
    MAX_REFINEMENT_DEPTH = 3

    def __init__(self, extraction_plan, workers: int = 1, tile_grid=None):
        super().__init__(
            f"Extracting {extraction_plan.dataset_id} "
            f"({extraction_plan.tile_count} tiles)",
            QgsTask.Flag.CanCancel,
        )
        self.plan = extraction_plan
        self.workers = max(1, int(workers))
        self._tile_grid = tile_grid
        self._rows: list[dict] = []
        self._features: list[dict] = []
        self._error: tuple[str, str] | None = None
        self._summary = ""
        self._failures: list[str] = []
        self._done = 0
        self._total = 0

    # -- the unit of work -------------------------------------------------

    def _fetch(self, chunk):
        """Fetch one tile.  Raises, so the runner can classify the failure."""
        chunk.request.region = earthengine.ring_to_ee(list(chunk.tile.ring))
        data = earthengine.extract(chunk.request)
        if isinstance(data, list):
            return plan_mod.tag_rows(data, chunk.tile)
        if isinstance(data, dict):
            key = "h3_index" if chunk.tile.is_indexed else "tile_id"
            for feature in data.get("features") or ():
                properties = feature.setdefault("properties", {})
                properties[key] = chunk.tile.id
                properties["tile_resolution"] = chunk.tile.resolution
            return data
        return []

    def _collect(self, result) -> None:
        if isinstance(result, list):
            self._rows.extend(result)
        elif isinstance(result, dict):
            self._features.extend(result.get("features") or ())

    # -- QgsTask ----------------------------------------------------------

    def run(self) -> bool:
        from ..core import concurrency

        pending = self.plan.chunks()
        self._total = len(pending)
        if not pending:
            self._error = ("Nothing to extract", "The plan produced no tiles.")
            return False

        seconds_per_request = self.plan.seconds_per_request
        depth = 0
        rate_limit_events = 0

        while pending and not self.isCanceled():

            def progress(outcome, done, total):
                overall = self._done + done
                self.setProgress(100.0 * overall / max(self._total, overall))
                if outcome.ok:
                    self._collect(outcome.result)

            report = concurrency.run_jobs(
                pending,
                self._fetch,
                workers=self.workers,
                progress=progress,
                is_cancelled=self.isCanceled,
            )
            self._done += len(report.outcomes)
            rate_limit_events += report.rate_limit_events
            if report.mean_seconds:
                seconds_per_request = report.mean_seconds

            if report.cancelled or self.isCanceled():
                self._summary = (
                    f"Cancelled after {self._done} of {self._total} tiles; "
                    f"{len(self._rows) + len(self._features):,} records kept"
                )
                break

            # Failures that will not be retried are reported, not swallowed.
            for outcome in report.failed:
                if not outcome.needs_subdivision:
                    self._failures.append(
                        f"{getattr(outcome.key, 'id', '?')}: {outcome.error}"
                    )

            oversized = [outcome.key for outcome in report.oversized]
            if not oversized:
                break

            depth += 1
            if depth > self.MAX_REFINEMENT_DEPTH:
                self._failures.extend(
                    f"{chunk.id}: still too large after "
                    f"{self.MAX_REFINEMENT_DEPTH} subdivisions"
                    for chunk in oversized
                )
                break

            finer: list = []
            for chunk in oversized:
                children = plan_mod.subdivide(chunk, self._tile_grid, len(finer))
                if children:
                    finer.extend(children)
                else:
                    self._failures.append(
                        f"{chunk.id}: too large and cannot be subdivided further"
                    )
            if not finer:
                break
            self.note.emit(
                f"{len(oversized)} tile(s) were refused as too large; "
                f"retrying them as {len(finer)} smaller tiles"
            )
            self._total += len(finer)
            pending = finer

        if not self._summary:
            self._summary = self._build_summary(seconds_per_request, rate_limit_events)
        if not self._rows and not self._features:
            self._error = (
                "No data was returned",
                self._failures[0]
                if self._failures
                else "Every tile came back empty. Widen the dates or move the "
                "area of interest, then try again.",
            )
            return False
        return True

    def _build_summary(self, seconds_per_request: float, rate_limit_events: int) -> str:
        from ..core import concurrency

        records = len(self._rows) or len(self._features)
        parts = [
            f"{records:,} records from {self._done} of {self._total} tiles",
            concurrency.describe_workers(self.workers),
            f"~{seconds_per_request:.1f}s per request",
        ]
        if rate_limit_events:
            parts.append(f"{rate_limit_events} rate-limit backoff(s)")
        if self._failures:
            parts.append(f"{len(self._failures)} tile(s) failed")
        return ", ".join(parts)

    @property
    def failures(self) -> list[str]:
        return list(self._failures)

    @property
    def data(self):
        """Rows for rasters, a GeoJSON feature collection for tables."""
        if self._features:
            return {"type": "FeatureCollection", "features": self._features}
        return self._rows

    def finished(self, result: bool) -> None:
        if result:
            self.completed.emit(self.data, self._summary)
        else:
            what, advice = self._error or ("Extraction failed", "")
            if self._failures:
                advice = (advice + "\n\n" + "\n".join(self._failures[:5])).strip()
            self.failed.emit(what, advice)
