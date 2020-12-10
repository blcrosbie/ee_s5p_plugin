# -*- coding: utf-8 -*-
"""
/***************************************************************************
 EarthEngineSentinel5P
                                 A QGIS plugin
 This plugin ports the Copernicus S5P Near Real Time and Offline L3 datasets found on Google Earth Engine
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2020-12-06
        copyright            : (C) 2020 by Brandon Crosbie
        email                : bcrosb31@gmail.com
        git sha              : $Format:%H$
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load EarthEngineSentinel5P class from file EarthEngineSentinel5P.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .ee_s5p_plugin import EarthEngineSentinel5P
    return EarthEngineSentinel5P(iface)


import os
import platform
import sys
import subprocess

if platform.system() == 'Windows':
    subprocess.check_call(['python', '-m', 'pip', 'install', 'beautifulsoup4'])
    subprocess.check_call(['python', '-m', 'pip', 'install', 'geojson'])
else:
    try:
        os.system("pip install beautifulsoup4")
    except:
        os.system("sudo pip install beautifulsoup4")


    try:
        os.system("pip3 install geojson")
    except:
        os.system("sudo pip3 install geojson")