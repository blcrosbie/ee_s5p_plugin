# coding=utf-8
"""DockWidget test.

.. note:: This program is free software; you can redistribute it and/or modify
     it under the terms of the GNU General Public License as published by
     the Free Software Foundation; either version 2 of the License, or
     (at your option) any later version.

"""

__author__ = 'bcrosb31@gmail.com'
__date__ = '2020-12-06'
__copyright__ = 'Copyright 2020, Brandon Crosbie'

import unittest

from qgis.PyQt.QtGui import QDockWidget

from ee_s5p_plugin_dockwidget import EarthEngineSentinel5PDockWidget

from utilities import get_qgis_app

QGIS_APP = get_qgis_app()


class EarthEngineSentinel5PDockWidgetTest(unittest.TestCase):
    """Test dockwidget works."""

    def setUp(self):
        """Runs before each test."""
        self.dockwidget = EarthEngineSentinel5PDockWidget(None)

    def tearDown(self):
        """Runs after each test."""
        self.dockwidget = None

    def test_dockwidget_ok(self):
        """Test we can click OK."""
        pass

if __name__ == "__main__":
    suite = unittest.makeSuite(EarthEngineSentinel5PDialogTest)
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)

