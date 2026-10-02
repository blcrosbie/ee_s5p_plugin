"""Earth Engine Catalog Query -- a QGIS plugin.

Browse the Google Earth Engine Data Catalog from QGIS, filter it by type, tags,
time and area, and extract data into text based formats that load straight onto
the map.

Began in 2020 as a tool for the Sentinel-5P atmospheric products; the S5P
datasets are still first-class, alongside the rest of the catalog.

Copyright (C) 2020-2026 Brandon Crosbie
This program is free software under the GNU General Public License v2 or later.
"""

from __future__ import annotations

__version__ = "1.1.0"
__author__ = "Brandon Crosbie"


# noinspection PyPep8Naming
def classFactory(iface):
    """Entry point QGIS calls to instantiate the plugin.

    :param iface: the running QGIS interface
    :type iface: qgis.gui.QgisInterface
    """
    from .plugin import EarthEngineCatalogPlugin

    return EarthEngineCatalogPlugin(iface)
