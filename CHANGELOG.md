# Changelog

This project follows [Semantic Versioning](https://semver.org).

## [1.0.0] — 2026-10-01

A rewrite for modern QGIS. The 2020 implementation is preserved unchanged on the
`master` branch.

### Coverage

- The entire Earth Engine Data Catalog — **1,155 datasets** (857 image
  collections, 178 images, 120 feature collections) from 110 providers — instead
  of a hand-maintained 92-dataset subset.
- Dataset metadata now comes from Google's published **STAC catalog** rather than
  from scraping catalog web pages with BeautifulSoup. The records are stable,
  carry band / visualisation / extent / licence metadata the old version had to
  guess at, and the crawl takes about eight seconds. The `beautifulsoup4`
  dependency is gone.
- 7 `bigquery_table` datasets are deliberately excluded and reported, rather than
  silently dropped: they cannot be opened through the Earth Engine Python API.
- The bundled snapshot (~800 KiB gzipped) makes browsing work offline on first
  run. A scheduled CI job re-crawls weekly and opens a PR when the catalog
  drifts.

### Qt 6 / QGIS 4

- Runs on both Qt 5 and Qt 6, so the plugin works on QGIS 3.22 through 4.x.
  Verified against QGIS 3.40.6 / Qt 6.8.1 and declared with `supportsQt6=True`
  and `qgisMaximumVersion=4.99`.
- Every unscoped Qt enum (`Qt.Checked`, `QMessageBox.Ok`, …) now uses the scoped
  spelling that Qt 6 requires and PyQt 5.15 accepts. No compatibility shim is
  needed; `tests/test_qt_compat.py` enforces the rules statically.
- The compiled `resources.py` is gone — PyQt 6 removed `pyrcc`, so a generated
  resource module would have made the plugin unloadable. The icon loads from its
  file path.
- All direct `PyQt5` imports replaced with `qgis.PyQt`.

### Responsiveness

- Catalog refreshes, dataset reads and extractions run as cancellable `QgsTask`
  background jobs with progress. Previously every network call ran on the GUI
  thread and froze QGIS, sometimes for minutes, with no way to cancel.
- Search is debounced, and the results table reuses one model over typed sort
  keys instead of rebuilding a `QStandardItemModel` row by row on each keystroke.

### Interface

- The dock is fully layout-managed and resizable. The old form positioned every
  widget at absolute pixel coordinates inside a 269-pixel-wide dock, so it could
  not be resized, ignored the user's font size, and could not grow to show a
  1,100-row list.
- Results are a sortable table showing id, type, availability and band count,
  with full metadata on hover. Each row carries its dataset record, rather than
  packing the id into a string and parsing it back out with `split("\t")`.
- Free-text search across id, title, publisher, tags, band names and description.
- Deprecated datasets (268 of them) are hidden by default and struck through when
  shown.
- A details panel renders the full description, band table with units and value
  ranges, licence, citation and palette swatches — from the local snapshot, so it
  works without Earth Engine credentials.
- Outcomes go to the QGIS message bar instead of interrupting with a modal dialog
  for every result, including successes.
- Areas of interest can come from a drawn shape, the features selected on a
  vector layer, or the visible canvas extent.

### Request sizing

- Request size is estimated **locally, before sending**, against the
  `getRegion` limit of 1,048,576 values, and the dock shows a live verdict as you
  change band, resolution, dates and area.
- When a request will not fit, the plugin suggests a resolution — derived from the
  inverse-square relationship between pixel size and point count — and a shorter
  date window. Previously the user discovered the limit only after a long failed
  round trip.
- Earth Engine failures are translated into plain language with a suggested next
  step; when the server reports exact figures, those replace the local estimate.

### Bugs fixed

- **CSV columns could silently misalign.** The header was taken from the first
  row's keys and values written positionally, so any row with a different key set
  was written under the wrong headings. Now the union of all keys is used and rows
  are written by name.
- **Pixel footprint polygons were about 30% too small.** The square generator
  scaled the radius by √2 and then halved it, while also treating the result as a
  circumradius. A 1,000 m pixel now produces a 1,000 m square.
- **Hex colour palettes were never recognised.** The check was
  `re.fullmatch("^[0-9a-fA-F]$", s)`, which matches only a *single* character, so
  every Earth Engine palette colour fell through to the "invalid" branch.
- **Areas of interest from projected layers landed in the wrong place.** Layer
  coordinates were passed to Earth Engine as degrees without reprojection.
  Geometry is now transformed to WGS84.
- **Date comparisons could raise `TypeError`.** Naive and timezone-aware datetimes
  were compared directly. All parsed dates are now timezone-aware UTC.
- **Ongoing collections were treated as closed.** Google publishes the newest
  ingested item's timestamp as a collection's end date, so time filtering against
  an aged snapshot hid current imagery. Actively updated collections are now
  flagged `ongoing` and treated as running to today.
- **A missing locale setting prevented the plugin loading.** `QSettings().value(
  "locale/userLocale")[0:2]` raised `TypeError` on a fresh profile where the
  setting is unset.
- **`plot_vector` referenced `self` from module scope** and would have raised
  `NameError` on its error path.
- **An unreachable `latitude` validator.** `QDoubleValidator(-90, -90, 9)` set the
  range to a single point, rejecting every latitude.
- **Point-in-polygon divided by zero** on rings with repeated latitudes.
- **Unknown distance units were accepted silently**, printing a note and using the
  raw number as kilometres.
- The toolbar button now toggles the dock and reflects its state, rather than
  re-adding a dock widget on every click.
- `unload()` now removes the dock widget and disconnects its signals, so the
  plugin can be cleanly reinstalled without restarting QGIS.

### Code quality

- Split the 2,887-line dock widget into a `core` package (no Qt, no QGIS, no
  `ee` — unit testable on plain Python) and a thin `gui` package. Both properties
  are enforced by tests.
- **371 unit tests** plus 6 opt-in live-catalog tests and a 30-check QGIS smoke
  test that loads the plugin and drives the dock. Previously there were no tests
  beyond the plugin-builder boilerplate.
- Every bare `except:` removed — there were dozens, and they are why failures
  surfaced as an empty result list with no explanation. A test now forbids them.
- Ruff lint and format clean, enforced in CI across Python 3.9 and 3.12 and
  against QGIS Qt 5 and Qt 6 container images.
- `plugin_upload.py`, which sent the OSGeo password over plain HTTP via
  `xmlrpclib`, replaced with an HTTPS-only uploader that reads credentials from
  the environment.
- Removed: unused `ee_catalog_details.py` (imported pandas, geopandas and shapely,
  none of which QGIS ships, and called an undefined `STOP`), the stale
  `gee_catalog_*.json` placeholders, committed `__pycache__`, a checked-in build
  zip, and the plugin-builder scaffolding.

### Packaging

- `tools/package.py` builds a reproducible zip and validates it the way
  plugins.qgis.org does: required metadata fields, no leftover placeholder URLs,
  matching versions in `metadata.txt` and `__init__.py`, required files present,
  and the size limit.
- `metadata.txt` filled in properly — the 2020 file still had `tracker=http://bugs`
  and `repository=http://repo`, which would have been rejected on upload — and the
  plugin is no longer flagged experimental.

## [0.1] — 2020-12

Initial release. Sentinel-5P NRTI and OFFL L3 products plus selected NOAA, NCEP,
WorldPop and feature collections; QGIS 3 on Qt 5. Written for a master's degree
project on marginal emissions factors.
