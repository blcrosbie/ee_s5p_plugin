# Changelog

This project follows [Semantic Versioning](https://semver.org).

## Unreleased

### Fixed

- **"Use selection" ignored selections on any layer but the active one**, which
  meant an area of interest silently stayed unset and extraction fell back to the
  dataset's own footprint. Found in QA: a polygon drawn and selected over Florida,
  with the TIGER counties layer active, extracted every county in the United
  States. The layer you select features on and the layer highlighted in the Layers
  panel are routinely different, so the selection is now taken from *every* vector
  layer that has one -- active layer first -- and each layer is reprojected with
  its own transform, since a selection can span layers in different CRSs.
- The area label now names the layers the selection came from
  (`Selection (1 on 'Test'): 980 km²`), so it is visible that the right area was
  picked up.
- "Nothing selected" now says no layer has a selection, rather than implying the
  wrong layer was active, and points at Draw area as the alternative.
- **Extracting with no area of interest is now an explicit warning about what
  would actually be downloaded**, naming the dataset. Tables get no size estimate
  -- feature density is not published -- so previously the only signal was "Size
  cannot be estimated", and a request for every county in the United States looked
  identical to a small one. For a table the warning now says it means every
  feature, and that Earth Engine truncates at 5,000 with no warning.

- **Extract raised a traceback when Earth Engine was not signed in.** Being able to
  import `ee` is not the same as having a session: the Google Earth Engine plugin
  bundles its own copy of `ee` on `sys.path`, which imports cleanly even when that
  plugin failed inside its own `classFactory` and never initialised. The
  availability check passed, and the first call then raised
  `EEException: Earth Engine client library not initialized` from deep inside `ee`,
  surfacing as a stack trace in the QGIS log.

  Every path that builds an `ee` object now goes through `require_initialised()`,
  which uses the public `ee.data.is_initialized()` (free, no round trip) and falls
  back to a cheap server call on older versions. Constructing even an
  `ee.Geometry` contacts the server to load API signatures, so there is no safe
  "offline" call to let through unchecked. The new `EarthEngineNotInitialised`
  exception is distinct from a missing install because the remedy is different, and
  the message gives the exact sign-in sequence.
- **The sign-in advice no longer walks the user into the next failure.** The Earth
  Engine plugin stores the Cloud project id *inside the credentials file*
  (`ee.oauth.get_credentials_path()`), and `ee.Authenticate()` rewrites that file
  from scratch rather than merging -- `write_private_json` removes it and writes a
  fresh dict -- so authenticating silently clears the project. The message now says
  to expect being asked for it again after restarting, where to find it, and that
  it has to be registered for Earth Engine. Same note added to the README.
- `explain_error()` now recognises the sign-in cases it was missing: not
  initialised, credentials absent or unusable, no Cloud project set, and a project
  not registered for Earth Engine -- each with what to do about it.

### Verified

- QGIS **3.44.15 LTR** (Qt 5.15.13, PyQt 5.15.11, Python 3.12.14): all 104 API
  symbols resolve, 83 smoke checks pass.
- QGIS **3.40.6** (Qt 6.8.1, PyQt 6.8.0, Python 3.12.10): same.

  The one code base running on both Qt majors is now confirmed on real builds
  rather than inferred.

### Documentation

- README states plainly that **there is no compile step**, with a table of what the
  0.1 build did (`pyrcc5`, `pyuic5`, `pb_tool`, `make`) and why each is gone. Coming
  from the old version, or from most QGIS plugin tutorials, a compile step is a
  reasonable thing to expect.
- Added how to replace an older install, and what `NewItemIOError ...
  ResourceExists` from the symlink command means.

- README now covers building the zip and installing it as a custom plugin:
  `tools/package.py`, *Install from ZIP*, what ends up in the archive and why
  hand-zipping the checkout does not work, the symlink-into-your-profile route for
  development, how to enable and find the plugin, and a troubleshooting table.

### Fixed

- **A virtual environment inside the checkout was being scanned.**
  `tests/test_docs.py` walked the repository root skipping only build and cache
  directories, so a `venv/` in the checkout pulled every file in `site-packages`
  into the checks -- tripling the suite from 667 tests to 1,965, and risking
  failures on third-party code. Virtual environments are now detected by their
  `pyvenv.cfg` marker, rather than by guessing at directory names, and a test
  asserts the collection stays the size of this repository.

- **Mangled Windows paths in the docs.** Instructions written through a shell
  heredoc had their backslash-b and backslash-a sequences interpreted as
  escapes, leaving a literal backspace and bell character in README.md and
  CONTRIBUTING.md. The rendered instruction named a launcher and a script that do
  not exist, so it was not a command anyone could run, and two of the four
  occurrences had already been committed.
  `tests/test_docs.py` now rejects stray control characters in any documentation or
  source file, checks that every `tools/*.py` the docs name actually exists, and
  checks that OSGeo4W launcher paths keep their `bin` segment. Verified by
  reintroducing the original corruption and watching the check fail.

### Fixed — QGIS 4 metadata

- **Removed `supportsQt6=True`.** It has been removed from QGIS core and is no
  longer recognised; plugins relying on it alone were dropped from the QGIS 4 list.
  `qgisMaximumVersion=4.99` is what puts a 3.x-floor plugin in the **QGIS 4 Ready**
  list, and that was already set. `tools/package.py` previously *required* the flag
  and now rejects it, and a test asserts it stays absent.
  ([official guide](https://plugins.qgis.org/docs/migrate-qgis4))

### Added — verifying against a new QGIS

- `tools/api_audit.py` resolves every name the plugin imports from `qgis.*`, and
  every dotted constant read off those names, against the running QGIS. A removed
  class or an enum member that moved namespace is how a plugin breaks on a new
  release, and this reports it in one command instead of at runtime.
- CI now runs the audit and the smoke test against **QGIS 3.22 (oldest claimed),
  3.44.15 LTR, 4.2.3, the moving `ltr` and `stable` channels, and master**,
  replacing a two-entry matrix. `qgis/qgis` stopped publishing `release-X_Y` tags
  after 3.36, so the current releases are tracked through `ltr` / `stable`; every
  tag in the matrix was checked to exist.

### Fixed

- **Finished tasks were never released.** `_tasks` was pruned with
  `sip.isdeleted()`, but `qgis.PyQt.sip` is not importable on every build and the
  guard silently returned False there, so the list grew for the session and
  `cancel_current_task` could call `.status()` on a destroyed object. Tasks are now
  dropped on `taskCompleted` / `taskTerminated`, with no sip dependency at all, and
  the cancel loop tolerates a task Qt has already destroyed. Found by the new audit.
- The test import hook defined `find_module` / `load_module`, removed from
  `importlib` in Python 3.12.

## [1.0.2] — 2026-10-01

Live progress feedback, for the heavy downloads the previous release made possible.

Auto-scaling turned a refused request into several hundred successful ones, which
left a new problem: a job that runs for minutes behind a bare percentage gives the
user no way to tell whether it is working, how much is left, or whether something
has gone wrong. The status panel answers all of that in one line:

```
Tile 143 of 545 · 28,600 records · 2.1 tiles/s · about 3 min 11 s left
```

### The panel

- Sits under the results list, shows itself only while something is running, and
  carries a **Cancel** button.
- Reports position, records fetched so far, measured throughput and remaining
  time.
- **The remaining time is measured, not predicted.** Once there are at least three
  completed tiles the estimate comes from actual throughput, so it corrects itself
  as the job proceeds. Before that it falls back to the rate the plan predicted,
  because a two-sample rate gives a wild answer.
- **Rate limiting is stated, not implied.** A throttled job shows "rate limited,
  backing off" instead of a bar that appears to have stalled — the one case where
  a stalled bar is correct behaviour and looks exactly like a hang.
- Splitting an oversized tile is reported, and the denominator grows to match.
- Failures appear as a running count, so a job with a few bad tiles does not look
  perfectly healthy until the end.
- A **single opaque request** — one big feature collection, say — gets a busy bar
  and a ticking elapsed time. Earth Engine reports no progress for these, so a
  percentage would be a lie; an elapsed counter at least shows it is alive.
- Cancellation is co-operative, and the panel says "Stopping…" rather than
  pretending the job has already ended while requests are still in flight.

### Implementation notes

- **No new dependencies.** Plain `QProgressBar`, `QLabel` and `QToolButton`, all
  already imported. Nothing was added for this that could complicate installation
  through the plugin repository.
- The statistics live in `core/progress.py` with no Qt import, so the wording and
  the arithmetic are unit tested; `gui/progress.py` is only the widget.
- Progress crosses from worker threads as a plain dict, which Qt's queued
  connections handle safely; widgets are only ever touched on the GUI thread.
- **Redraws are throttled to five a second.** A 500 tile job with eight workers
  would otherwise repaint the dock faster than it fetches, competing with the
  threads doing the work. The first and last updates always get through, so the
  panel never settles on a stale partial state.

### Fixed

- `ProgressTracker`'s start time used `default_factory=time.perf_counter`, which
  binds the original function at class-creation time and so could not be
  substituted in tests. Deferred to call time.

## [1.0.1] — 2026-10-01

Auto-scaling large areas into H3 tiles, with optional parallel fetching.

### Auto-scale

Earth Engine answers at most 1,048,576 values per `getRegion` call (and 5,000
features per table call), so a whole US state of Sentinel-5P at native resolution
cannot be fetched in one request. Rather than refusing, the plugin now offers to
tile it.

- The tiling resolution is derived **arithmetically** from the limit, not found by
  probing: point count scales as the inverse square of the ground resolution, so
  the per-tile area budget is `limit x (resolution/1000)² / (bands x images)`. The
  search then walks H3 resolutions from coarse to fine and takes the first that
  fits — coarse is better, because it means fewer round trips. Planning is instant
  and costs no quota.
- The dialog shows what it will cost before anything is sent: tile count, tile
  size with a human comparison ("about 253 km² per cell, a metropolitan area"),
  per-tile value estimate, and the estimated wall-clock time. The resolution can
  be overridden either way.
- A short-circuit comes first: if the whole area already fits, nothing is tiled.

Worked example — Pennsylvania, 3 bands, at 1,113 m:

| Request | Tiling | Tiles |
|---|---|---|
| 1 day | not needed | 1 |
| 1 month | H3 res 4 (~1,770 km²) | 84 |
| 1 year | H3 res 5 (~253 km²) | 545 |

### Parallel fetching

- Worker count is selectable from 1 to **half the machine's CPUs**, capped at 8
  because past that Earth Engine throttles anyway. The dialog shows the predicted
  time and speed-up for each choice, and the selection is remembered.
- **Sequential is the default.** It is a little slower but cannot be rate limited
  for concurrency, and it is one less thing to reason about when something fails.
- These are **threads, not processes**: an Earth Engine request is almost entirely
  an HTTPS round trip, Python releases the GIL while waiting, and threads avoid
  re-importing QGIS per worker and pickling `ee` objects. The CPU-based cap is
  kept because it is the intuitive dial and stops the plugin monopolising a
  laptop, but the real ceiling is usually Earth Engine's rate limit.
- Measured latency replaces the estimate as a job runs, so the reported time
  converges on the truth.

### Rate limits and refusals

- HTTP 429, quota and "too many concurrent" errors back off exponentially with
  jitter — without jitter, parallel workers retry in lockstep and hit the server
  again together.
- Backoff is **shared**: rate limiting applies to the account, not to one request,
  so a 429 narrows the concurrency allowance for every worker and pauses them all,
  then widens again after a run of successes. A throttled job slows down instead
  of failing.
- "Too many values" and "user memory limit exceeded" are classified as *too large*
  rather than transient, so they are subdivided instead of retried unchanged —
  retrying an over-large request only burns attempts. A refused tile is split one
  H3 resolution finer (seven children, exactly conserving area) and retried, up to
  three times.
- A tile that fails permanently is reported with its id, not swallowed.

### H3

- Tiles are real H3 cells when the `h3` package is installed, and every extracted
  row carries `h3_index` and `tile_resolution`, so output joins against anything
  else keyed by H3 — including `h3-js` on the web side.
- `h3` is a compiled extension that QGIS does not bundle, so it is **optional**.
  Without it the plugin tiles with an equivalent latitude/longitude grid, which
  covers the area just as exactly, and says plainly that the ids are not real H3
  indexes. Nothing is ever installed automatically — the 2020 version pip-installed
  BeautifulSoup behind the user's back, which is exactly what not to do.
- Both the 4.x (`geo_to_cells`) and 3.x (`polyfill`) APIs are supported, and the
  hardcoded area table is verified against the live library by a test.

### Fixed along the way

- **A small area could silently extract nothing.** H3 selects cells by *centre*
  containment, so an area smaller than one cell yields zero cells. That read as a
  successful run returning no data; the area itself is now used as a single tile.
- **Durations measured as zero on Windows.** `time.monotonic()` has ~16 ms
  granularity there, so a fast request recorded 0.0 s and poisoned the running
  average. Durations now use `perf_counter`.
- Refinement could have looped forever on a tile that cannot be subdivided
  further; it now detects that and stops.

### Also

- Collection presets next to the search box, led by **Sentinel-5P / TROPOMI** —
  the products this plugin was written for and still its main audience. The
  selection is remembered; the default remains the whole catalog.
- 546 unit tests (up from 371) and 59 QGIS smoke checks (up from 30), including a
  full chunked run against a stubbed Earth Engine, sequential and in parallel, with
  subdivision and permanent-failure paths.

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
