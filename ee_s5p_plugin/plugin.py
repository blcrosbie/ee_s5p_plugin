"""QGIS plugin entry point: menu, toolbar and dock lifecycle.

Earth Engine Catalog Query -- browse the Google Earth Engine Data Catalog from
QGIS and extract data from it.

Copyright (C) 2020-2026 Brandon Crosbie
Licensed under the GNU General Public License v2 or later.
"""

from __future__ import annotations

import contextlib
import os

from qgis.core import Qgis
from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .gui import messages
from .gui.dockwidget import EarthEngineCatalogDockWidget

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MENU_TITLE = "&Earth Engine Catalog"


class EarthEngineCatalogPlugin:
    """Implements the interface QGIS expects of a plugin."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = PLUGIN_DIR
        self.actions: list[QAction] = []
        self.dockwidget: EarthEngineCatalogDockWidget | None = None
        self._translator: QTranslator | None = None
        self._install_translator()

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def _install_translator(self) -> None:
        locale = QSettings().value("locale/userLocale")
        # A fresh profile can leave this unset; the original indexed [0:2] on it
        # unconditionally and raised TypeError before the plugin could load.
        if not isinstance(locale, str) or len(locale) < 2:
            return
        path = os.path.join(
            self.plugin_dir, "i18n", f"EarthEngineCatalog_{locale[:2]}.qm"
        )
        if not os.path.exists(path):
            return
        translator = QTranslator()
        if translator.load(path):
            QCoreApplication.installTranslator(translator)
            self._translator = translator

    def tr(self, message: str) -> str:
        return QCoreApplication.translate("EarthEngineCatalog", message)

    # ------------------------------------------------------------------
    # QGIS interface
    # ------------------------------------------------------------------

    def initGui(self) -> None:
        """Create the toolbar button and menu entry."""
        # The icon is loaded from the file rather than a compiled Qt resource
        # module: PyQt6 dropped pyrcc, so a generated resources.py would make the
        # plugin unloadable on QGIS builds using Qt 6.
        icon = QIcon(os.path.join(self.plugin_dir, "icon.png"))
        action = QAction(icon, self.tr("Earth Engine Catalog"), self.iface.mainWindow())
        action.setObjectName("EarthEngineCatalogAction")
        action.setStatusTip(
            self.tr("Browse the Earth Engine Data Catalog and extract data")
        )
        action.setWhatsThis(
            self.tr(
                "Search Google's Earth Engine Data Catalog, filter it by type, "
                "tags, time and area, then extract data into QGIS."
            )
        )
        action.setCheckable(True)
        action.triggered.connect(self.toggle)

        self.iface.addToolBarIcon(action)
        self.iface.addPluginToMenu(self.tr(MENU_TITLE), action)
        self.actions.append(action)

    def unload(self) -> None:
        """Remove everything the plugin added, so it can be cleanly reinstalled."""
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(MENU_TITLE), action)
            self.iface.removeToolBarIcon(action)
        self.actions.clear()

        if self.dockwidget is not None:
            # TypeError here just means it was already disconnected.
            with contextlib.suppress(TypeError):
                self.dockwidget.closingPlugin.disconnect(self._on_dock_closed)
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()
            self.dockwidget = None

    # ------------------------------------------------------------------
    # Dock handling
    # ------------------------------------------------------------------

    def toggle(self, checked: bool) -> None:
        """Toolbar button: show the dock, or hide it if it is already up."""
        if not checked:
            if self.dockwidget is not None:
                self.dockwidget.hide()
            return
        self.show()

    def show(self) -> None:
        if self.dockwidget is None:
            try:
                self.dockwidget = EarthEngineCatalogDockWidget(iface=self.iface)
            except Exception as error:
                # A plugin that raises here is simply dead with no explanation in
                # the UI, so report it and leave QGIS usable.
                messages.log(
                    f"Could not create the dock widget: {error}",
                    level=Qgis.MessageLevel.Critical,
                )
                messages.push_error(
                    self.iface,
                    self.tr("Earth Engine Catalog failed to open: {error}").format(
                        error=error
                    ),
                )
                self._set_checked(False)
                return
            self.dockwidget.closingPlugin.connect(self._on_dock_closed)
            self.iface.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget
            )

        self.dockwidget.show()
        self.dockwidget.raise_()
        self._set_checked(True)

    def _on_dock_closed(self) -> None:
        self._set_checked(False)

    def _set_checked(self, checked: bool) -> None:
        for action in self.actions:
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)
