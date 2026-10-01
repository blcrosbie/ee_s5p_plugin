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
