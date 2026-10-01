# Earth Engine Catalog Query

A QGIS plugin for browsing the whole [Google Earth Engine Data Catalog][catalog]
and pulling data out of it onto the map — without leaving QGIS, and without
copying dataset ids out of web pages.

[catalog]: https://developers.google.com/earth-engine/datasets

```
Search ───▶ Filter (type · provider · tags · time · area) ───▶ Inspect ───▶ Extract ───▶ Layer
```

## What it does

- **Searches all 1,155 datasets** by id, title, publisher, tag, band name or
  description. The list ships with the plugin, so browsing works offline, and
  refreshes from Google's published STAC catalog in about eight seconds.
- **Filters** by dataset type, provider, tags (any or all), date range and area of
  interest. Deprecated datasets are hidden by default and shown struck through
  when you ask for them.
- **Shows you what you're about to request**: bands with units and value ranges,
  availability and update cadence, native resolution, licence, citation, and
  Google's own suggested colour palette.
- **Estimates the request before sending it.** Earth Engine refuses more than
  1,048,576 values from `getRegion`; the plugin works out locally whether your
  request will fit, and if not, suggests a resolution or a date window that
  would.
- **Writes GeoJSON, CSV or JSON** and loads the result straight onto the map, with
  the dataset's palette applied on request.
- **Never blocks QGIS.** Catalog refreshes and extractions run as cancellable
  background tasks.

Image collections, single images and feature collections are all supported.

## Installing

### From the QGIS Plugin Repository

*Plugins → Manage and Install Plugins → search for "Earth Engine Catalog Query".*

### From a zip

Download `ee_s5p_plugin-<version>.zip` from the [releases page][releases], then
*Plugins → Manage and Install Plugins → Install from ZIP*.

[releases]: https://github.com/blcrosbie/ee_s5p_plugin/releases

### Requirements

| | |
|---|---|
| QGIS | 3.22 or newer, including 4.x — works on both Qt 5 and Qt 6 builds |
| For browsing | nothing beyond QGIS |
| For extracting | the [**Google Earth Engine**][ee-plugin] plugin, which provides the `ee` Python API and handles authentication |

[ee-plugin]: https://plugins.qgis.org/plugins/ee_plugin/

You also need a Google account [registered for Earth Engine][register]. Recent
Earth Engine API versions require a Cloud project as well; the Google Earth
Engine plugin walks you through both.

[register]: https://code.earthengine.google.com/register

## Using it

1. Click the toolbar icon to open the dock.
2. Type in the search box, or narrow things down on the **Type**, **Tags**,
   **Time** and **Area** tabs.
3. Select a dataset. Double-click it, or press **Details…**, to read its full
   metadata.
4. Pick a band and a resolution — **Native** fills in the dataset's own ground
   resolution.
5. Set an area of interest on the **Area** tab. This matters: most datasets are
   global, and a global request at native resolution will be refused. You can
   draw a square or hexagon around a coordinate, use the features selected on a
   vector layer, or use the visible map extent.
6. Watch the estimate line. If it says your request is over the limit, take the
   suggestion it offers.
7. Press **Extract…**, choose where to save, and the result is added as a layer.

**Style layer** shades the extracted layer using the palette Google publishes for
that band.

## How it finds datasets

Google publishes [STAC][stac] metadata for every Earth Engine dataset. The plugin
crawls it two levels deep — root catalog → one catalog per provider → one record
per dataset — and reduces each record to the fields it needs.

[stac]: https://stacspec.org

The 2020 version of this plugin scraped the catalog's HTML pages with
BeautifulSoup, which covered 92 datasets and broke whenever the page markup
changed. The STAC records are stable, carry far more metadata, and need no
third-party dependency.

Seven `bigquery_table` datasets are deliberately excluded: they live in BigQuery
and cannot be opened through the Earth Engine Python API at all, so listing them
would only offer requests that are guaranteed to fail.

### Keeping it fresh

| | |
|---|---|
| In the plugin | the refresh button re-crawls the catalog and caches it in your QGIS profile. You're prompted when the snapshot is more than 30 days old. |
| In this repository | a scheduled GitHub Action re-crawls weekly and opens a pull request when the catalog has drifted, so each release ships current data. |

`end` dates need care: Google publishes the timestamp of the newest *ingested*
item as the end of a collection's temporal extent, so an actively updated
collection looks closed as soon as the snapshot ages. Anything not deprecated
whose newest item is recent is flagged `ongoing`, and the time filter then treats
its end as "today" rather than as a hard boundary. Without that, a week-old
snapshot would hide today's imagery.

## Development

```bash
pip install -r requirements-dev.txt

pytest tests              # 371 tests, no QGIS needed
pytest -m network         # also hit the live Earth Engine catalog
ruff check . && ruff format --check .

python tools/smoke_test.py       # load the plugin in a real QGIS
python tools/refresh_catalog.py  # regenerate the bundled snapshot
python tools/package.py          # build the installable zip
```

On Windows, run the smoke test with a QGIS Python:

```
C:\OSGeo4W\bin\python-qgis-ltr-qt6.bat tools\smoke_test.py
```

### Layout

```
ee_s5p_plugin/
  core/        no Qt, no QGIS, no ee — unit tested on plain Python
    stac.py      crawl and normalise Google's STAC catalog
    catalog.py   Dataset records, indexes, filtering
    store.py     bundled snapshot + user cache, freshness
    dates.py     the several date formats Earth Engine mixes
    geometry.py  buffers, areas, containment
    request.py   validation, local size estimation, error interpretation
    export.py    GeoJSON / CSV / JSON writers
    earthengine.py  the only module that imports `ee`
  gui/         Qt and QGIS live here
    dockwidget.py   the dock, built in code (no .ui file)
    catalog_model.py  table model over the result set
    tasks.py        QgsTask subclasses for background work
    layers.py       map layers, geometry bridging, styling
    details.py      metadata as HTML
    messages.py     message bar and dialogs
  data/
    gee_catalog.json.gz   the bundled snapshot (~800 KiB)
```

`core` stays free of Qt and `ee` on purpose: it makes the interesting logic
testable without a QGIS install, and it means a missing Earth Engine
installation cannot stop you browsing the catalog. Both properties are enforced
by tests in `tests/test_qt_compat.py`.

### Supporting Qt 5 and Qt 6 at once

No compatibility shim is needed, because PyQt 5.15 accepts the fully scoped enum
spelling that PyQt 6 *requires* — but only if every call site uses it. So:

| Don't | Do |
|---|---|
| `Qt.Checked` | `Qt.CheckState.Checked` |
| `QMessageBox.Ok` | `QMessageBox.StandardButton.Ok` |
| `widget.exec_()` | `widget.exec()` |
| `QRegExp` | `QRegularExpression` |
| `from PyQt5 import …` | `from qgis.PyQt import …` |
| a compiled `resources.py` | load the icon from its file path |

`tests/test_qt_compat.py` enforces all of this statically, so the port cannot
regress unnoticed.

## History

Written in 2020 as part of a master's degree, to collect atmospheric
measurements from Sentinel-5P and calculate marginal emissions factors. That
version is preserved unchanged on the [`master`][master] branch.

[master]: https://github.com/blcrosbie/ee_s5p_plugin/tree/master

`main` is the 2026 rewrite: the whole catalog instead of a subset, Qt 6 support,
background tasks, local request estimation, and a test suite. See
[CHANGELOG.md](CHANGELOG.md) for the details, including the bugs it fixes.

## Licence

GNU General Public License v2 or later — see [LICENSE](LICENSE).

Dataset metadata comes from Google's Earth Engine catalog and remains subject to
each dataset's own terms of use, which the details panel shows. Earth Engine
itself is governed by Google's [terms of service][ee-terms].

[ee-terms]: https://earthengine.google.com/terms/

Brandon Lee Crosbie
