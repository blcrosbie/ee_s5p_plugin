"""Message helpers, and the one place Qt's scoped enums are spelled out.

Qt 6 removed the unscoped enum aliases (``QMessageBox.Ok``), so every such name
has to be written in full (``QMessageBox.StandardButton.Ok``).  PyQt 5.15 accepts
the full form too, which is what lets one code base serve QGIS 3.22 through 4.x
without a shim -- but repeating those names at forty call sites would be
miserable, so the dialogs live here instead.

Progress and status go to the QGIS message bar rather than modal pop-ups: the
original plugin interrupted the user with a modal box for every outcome,
including successes.
"""

from __future__ import annotations

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtWidgets import QMessageBox

#: Shown in the QGIS log panel and as the message-bar title.
LOG_TAG = "Earth Engine Catalog"

_ICON = QMessageBox.Icon
_BUTTON = QMessageBox.StandardButton


def log(message: str, level: int = Qgis.MessageLevel.Info) -> None:
    """Append to the QGIS log panel under :data:`LOG_TAG`."""
    QgsMessageLog.logMessage(str(message), LOG_TAG, level=level)


# ---------------------------------------------------------------------------
# Message bar (non-blocking)
# ---------------------------------------------------------------------------


def push(
    iface,
    message: str,
    level: int = Qgis.MessageLevel.Info,
    duration: int = 5,
    title: str = LOG_TAG,
) -> None:
    """Non-blocking notice in the QGIS message bar."""
    if iface is None:
        log(message, level)
        return
    iface.messageBar().pushMessage(title, str(message), level=level, duration=duration)


def push_success(iface, message: str, duration: int = 5) -> None:
    push(iface, message, Qgis.MessageLevel.Success, duration)


def push_warning(iface, message: str, duration: int = 8) -> None:
    log(message, Qgis.MessageLevel.Warning)
    push(iface, message, Qgis.MessageLevel.Warning, duration)


def push_error(iface, message: str, duration: int = 0) -> None:
    """``duration=0`` leaves the bar up until the user dismisses it."""
    log(message, Qgis.MessageLevel.Critical)
    push(iface, message, Qgis.MessageLevel.Critical, duration)


# ---------------------------------------------------------------------------
# Modal dialogs (only where an answer is actually needed)
# ---------------------------------------------------------------------------


def _dialog(parent, title: str, text: str, detail: str | None, icon) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    return box


def info(parent, title: str, text: str, detail: str | None = None) -> None:
    box = _dialog(parent, title, text, detail, _ICON.Information)
    box.setStandardButtons(_BUTTON.Ok)
    box.exec()


def warn(parent, title: str, text: str, detail: str | None = None) -> None:
    box = _dialog(parent, title, text, detail, _ICON.Warning)
    box.setStandardButtons(_BUTTON.Ok)
    box.exec()


def error(parent, title: str, text: str, detail: str | None = None) -> None:
    box = _dialog(parent, title, text, detail, _ICON.Critical)
    box.setStandardButtons(_BUTTON.Ok)
    box.exec()


def confirm(
    parent, title: str, text: str, detail: str | None = None, default_yes: bool = False
) -> bool:
    """Yes/No question. Returns ``True`` only for an explicit Yes."""
    box = _dialog(parent, title, text, detail, _ICON.Question)
    box.setStandardButtons(_BUTTON.Yes | _BUTTON.No)
    box.setDefaultButton(_BUTTON.Yes if default_yes else _BUTTON.No)
    return box.exec() == _BUTTON.Yes


def long_text(parent, title: str, html: str) -> None:
    """Show a long description in a resizable, scrollable dialog.

    The original built a ``QScrollArea`` into a ``QMessageBox`` and pinned it to
    1080x800 with a stylesheet, which overflowed small screens.  A plain dialog
    with a text browser scales properly and allows selecting and copying text.
    """
    # Imported here so the common paths above do not pull in the extra widgets.
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QTextBrowser,
        QVBoxLayout,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumSize(560, 420)
    layout = QVBoxLayout(dialog)

    browser = QTextBrowser(dialog)
    browser.setOpenExternalLinks(True)
    browser.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextBrowserInteraction
        | Qt.TextInteractionFlag.TextSelectableByMouse
    )
    browser.setHtml(html)
    layout.addWidget(browser)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    dialog.exec()
