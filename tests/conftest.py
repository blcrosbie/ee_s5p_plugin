"""Make the plugin's ``core`` package importable without QGIS on the path.

The plugin ships as ``ee_s5p_plugin/`` and is imported by QGIS as a package.  The
``core`` modules deliberately have no Qt / QGIS / ``ee`` dependency, so the tests
import them directly from the plugin directory.
"""

from __future__ import annotations

import os
import sys

PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ee_s5p_plugin"
)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
