"""Make the plugin's ``core`` package importable without QGIS on the path.

The plugin ships as ``ee_s5p_plugin/`` and is imported by QGIS as a package.  The
``core`` modules deliberately have no Qt / QGIS / ``ee`` dependency, so the tests
import them directly from the plugin directory.

Set ``EE_PLUGIN_NO_H3=1`` to run the whole suite as though the optional ``h3``
package were not installed.  That is the configuration most QGIS users are in,
since QGIS does not bundle ``h3``, so it needs to be as well tested as the other
one -- CI runs both.
"""

from __future__ import annotations

import os
import sys

PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ee_s5p_plugin"
)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


class _BlockH3:
    """Import hook that makes ``import h3`` raise ImportError."""

    def find_module(self, fullname, path=None):  # legacy API, harmless
        return self if fullname == "h3" or fullname.startswith("h3.") else None

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "h3" or fullname.startswith("h3."):
            raise ImportError(f"No module named {fullname!r} (blocked by the tests)")
        return

    def load_module(self, fullname):
        raise ImportError(f"No module named {fullname!r} (blocked by the tests)")


if os.environ.get("EE_PLUGIN_NO_H3"):
    for name in [n for n in sys.modules if n == "h3" or n.startswith("h3.")]:
        del sys.modules[name]
    sys.meta_path.insert(0, _BlockH3())


def pytest_report_header(config):
    try:
        import h3
    except ImportError:
        return "h3: not available (fallback tiling will be exercised)"
    return f"h3: {h3.__version__} (real H3 tiling will be exercised)"
