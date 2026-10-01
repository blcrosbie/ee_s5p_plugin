"""Table model for the dataset results list.

v0.1 rebuilt a ``QStandardItemModel`` row by row on every filter change, with the
dataset id squeezed into one string and parsed back out with ``split("\\t")``
whenever it was needed.  That cannot carry 1100 datasets, and it loses the record.

Here each row *is* a :class:`~...core.catalog.Dataset`: the model hands the real
object back through :data:`DATASET_ROLE`, so nothing has to be re-parsed, and
sorting is done on typed sort keys rather than on display strings.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QFont

from ..core import dates, stac
from ..core.catalog import Dataset

#: Retrieves the :class:`Dataset` behind a row.
DATASET_ROLE = int(Qt.ItemDataRole.UserRole) + 1

_DISPLAY = Qt.ItemDataRole.DisplayRole
_TOOLTIP = Qt.ItemDataRole.ToolTipRole
_FONT = Qt.ItemDataRole.FontRole
_ALIGN = Qt.ItemDataRole.TextAlignmentRole

COLUMN_DATASET = 0
COLUMN_TYPE = 1
COLUMN_AVAILABILITY = 2
COLUMN_BANDS = 3

_HEADERS = ("Dataset", "Type", "Available", "Bands")

_SHORT_TYPE = {
    "ImageCollection": "Collection",
    "Image": "Image",
    "FeatureCollection": "Table",
}


class DatasetTableModel(QAbstractTableModel):
    """Flat, sortable table over a list of datasets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._datasets: list[Dataset] = []

    # -- population -------------------------------------------------------

    def set_datasets(self, datasets) -> None:
        self.beginResetModel()
        self._datasets = list(datasets)
        self.endResetModel()

    def dataset_at(self, row: int) -> Dataset | None:
        if 0 <= row < len(self._datasets):
            return self._datasets[row]
        return None

    @property
    def datasets(self) -> list[Dataset]:
        return list(self._datasets)

    # -- QAbstractTableModel ---------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._datasets)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=_DISPLAY):
        if role != _DISPLAY or orientation != Qt.Orientation.Horizontal:
            return None
        return _HEADERS[section] if 0 <= section < len(_HEADERS) else None

    def data(self, index, role=_DISPLAY):
        if not index.isValid():
            return None
        dataset = self.dataset_at(index.row())
        if dataset is None:
            return None
        column = index.column()

        if role == DATASET_ROLE:
            return dataset
        if role == _DISPLAY:
            return self._display(dataset, column)
        if role == _TOOLTIP:
            return self._tooltip(dataset)
        if role == _FONT and dataset.deprecated:
            # Deprecated datasets stay visible but are struck through, so an old
            # analysis can still be reproduced without the id looking current.
            font = QFont()
            font.setStrikeOut(True)
            return font
        if role == _ALIGN and column == COLUMN_BANDS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _display(dataset: Dataset, column: int):
        if column == COLUMN_DATASET:
            return dataset.id
        if column == COLUMN_TYPE:
            return _SHORT_TYPE.get(dataset.type, dataset.type)
        if column == COLUMN_AVAILABILITY:
            start = dates.to_ee_date(dataset.start) or "?"
            if dataset.ongoing:
                return f"{start} →"
            end = dates.to_ee_date(dataset.end)
            return f"{start} – {end}" if end else start
        if column == COLUMN_BANDS:
            return str(len(dataset.bands)) if dataset.bands else ""
        return None

    @staticmethod
    def _tooltip(dataset: Dataset) -> str:
        lines = [f"<b>{dataset.title}</b>", f"<code>{dataset.id}</code>"]
        if dataset.publisher:
            lines.append(f"Publisher: {dataset.publisher}")
        lines.append(f"Available: {dataset.availability}")
        if dataset.cadence:
            lines.append(f"Updated: {dataset.cadence}")
        resolution = dataset.default_resolution()
        if resolution:
            lines.append(f"Native resolution: {resolution:g} m")
        if dataset.deprecated:
            lines.append("<b>Deprecated by Google</b>")
        summary = stac.plain_text(dataset.description, limit=320)
        if summary:
            lines.append(f"<br>{summary}")
        return "<br>".join(lines)

    # -- sorting ----------------------------------------------------------

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """Sort on typed keys, not on the rendered strings."""
        reverse = order == Qt.SortOrder.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        self._datasets.sort(key=lambda d: _sort_key(d, column), reverse=reverse)
        self.layoutChanged.emit()


def _sort_key(dataset: Dataset, column: int):
    if column == COLUMN_TYPE:
        return (dataset.type, dataset.id)
    if column == COLUMN_AVAILABILITY:
        # Collections still growing sort after everything with a fixed end.
        parsed = dates.parse(dataset.start)
        return (parsed.timestamp() if parsed else 0.0, dataset.id)
    if column == COLUMN_BANDS:
        return (len(dataset.bands), dataset.id)
    return dataset.id.lower()
