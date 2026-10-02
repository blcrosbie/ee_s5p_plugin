# Earth Engine Catalog Query

A QGIS plugin for browsing the [Google Earth Engine Data Catalog][catalog] and
pulling data out of it onto the map — without leaving QGIS, and without copying
dataset ids out of web pages.

Built around the **Sentinel-5P / TROPOMI** atmospheric products, which get a
one-click preset, and extended to the other 1,100+ datasets in the catalog.

[catalog]: https://developers.google.com/earth-engine/datasets

```
Search ──▶ Filter (type · provider · tags · time · area) ──▶ Inspect ──▶ Extract ──▶ Layer
                                                                  │
                                            too large for one call ▼
                                   Auto-scale: H3 tiles ──▶ fetch (1..N workers) ──▶ Layer
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
- **Auto-scales what won't fit.** A whole US state of Sentinel-5P cannot be
  fetched in one call. The plugin tiles it into [H3][h3] hexagons — picking the
  coarsest tiling that fits, so the request count stays low — shows you what it
  will cost, then fetches the tiles. Every row carries its `h3_index`.
- **Optional parallel fetching**, 1 to half your CPUs, with the predicted speed-up
  shown per choice. Rate limits are absorbed with backoff; tiles Earth Engine
  still refuses are split finer and retried.
- **Tells you what it's doing.** A status panel reports position, records fetched,
  throughput and a remaining-time estimate measured from actual throughput — plus a
  Cancel button. Rate limiting says so rather than looking like a hang.
- **Never blocks QGIS.** Catalog refreshes and extractions run as cancellable
  background tasks.

[h3]: https://h3geo.org

Image collections, single images and feature collections are all supported.

## Installing

### From the QGIS Plugin Repository

*Plugins → Manage and Install Plugins → search for "Earth Engine Catalog Query".*

### From a zip

Download `ee_s5p_plugin-<version>.zip` from the [releases page][releases], then in
QGIS: *Plugins → Manage and Install Plugins… → **Install from ZIP*** → pick the
file → **Install Plugin**.

[releases]: https://github.com/blcrosbie/ee_s5p_plugin/releases

### Building the zip yourself

**There is no compile step.** If you are used to QGIS plugins built with
`pb_tool compile`, `make`, `pyuic5` or `pyrcc5` -- as version 0.1 of this plugin
was -- none of that applies here:

| Old step | Why it is gone |
|---|---|
| `pyrcc5 resources.qrc -o resources.py` | PyQt6 removed `pyrcc` altogether, so a compiled resource module would make the plugin fail to load on QGIS 4. The icon is loaded from its file path instead. |
| `pyuic5` / loading a `.ui` file | The dock is built in Python. The old form placed every widget at fixed pixel coordinates, so it could not be resized or hold a long result list, and a generated file can silently drift from the code. |

So the whole build is one command, from a clone of this repository:

```bash
python tools/package.py
```

That writes `dist/ee_s5p_plugin-<version>.zip` and prints the file count, size and
version. Then install it with *Install from ZIP* as above.

**Don't zip the repository by hand.** QGIS requires the archive to contain exactly
one top-level directory holding `metadata.txt`, and zipping the checkout would put
`tests/`, `tools/` and `pyproject.toml` in there alongside it — at which point QGIS
either rejects it or installs something that cannot load. `tools/package.py` builds
the right shape and refuses to build a broken one: it checks the required metadata
fields, that `metadata.txt` and `__init__.py` agree on the version, that the
bundled catalog is present and not a stub, and that the result is under the
25 MiB limit.

To check without building:

```bash
python tools/package.py --validate
```

<details>
<summary>What ends up in the zip</summary>

```
ee_s5p_plugin/
├── metadata.txt            what QGIS reads to list the plugin
├── __init__.py             classFactory, the entry point QGIS calls
├── plugin.py               menu, toolbar, dock lifecycle
├── icon.png
├── LICENSE
├── core/                   no Qt, no QGIS, no ee
├── gui/                    the dock, dialogs, background tasks
└── data/
    └── gee_catalog.json.gz the bundled dataset list (~800 KiB)
```

Tests, tooling and CI config are deliberately excluded — they are not needed at
runtime and would only inflate the download.
</details>

### Installing from a folder instead (for development)

Skipping the zip and symlinking the package into your QGIS profile means your edits
are live — no rebuild, no reinstall. Find the right folder from inside QGIS via
*Settings → User Profiles → **Open Active Profile Folder***, which is correct on
every version and platform, then use its `python/plugins` subfolder.

```powershell
# Windows, in an elevated PowerShell, from the repository root
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ee_s5p_plugin" `
  -Target "$PWD\ee_s5p_plugin"
```

```bash
# Linux
PROFILE=~/.local/share/QGIS/QGIS3/profiles/default
ln -s "$PWD/ee_s5p_plugin" "$PROFILE/python/plugins/ee_s5p_plugin"

# macOS
PROFILE=~/"Library/Application Support/QGIS/QGIS3/profiles/default"
ln -s "$PWD/ee_s5p_plugin" "$PROFILE/python/plugins/ee_s5p_plugin"
```

Copying the `ee_s5p_plugin/` folder there works too, but then you have to re-copy
after every change.

If `New-Item` fails with `NewItemIOError ... ResourceExists`, something is already
at that path -- usually a previous install. Remove it first with the `Remove-Item`
command above, then create the link.

### Replacing an older install

*Install from ZIP* overwrites the folder of the same name, so installing over an
earlier version works. A 0.1 install leaves orphans behind though -- `resources.py`,
`scripts/`, `pb_tool.cfg` and friends, none of which 1.x uses -- so it is tidier to
delete the old folder first:

```powershell
Remove-Item -Recurse -Force "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ee_s5p_plugin"
```

Restart QGIS afterwards, or reload the plugin with [Plugin Reloader][reloader].

Use either the zip or the symlink below, not both: whichever was written to the
profile last is the one QGIS loads.

### Turning it on, and finding it

*Plugins → Manage and Install Plugins… → **Installed*** → tick **Earth Engine
Catalog Query**. *Install from ZIP* usually enables it for you.

It then appears as a toolbar button, and under *Plugins → Earth Engine Catalog*.
The button toggles a dock panel on the right.

### If it doesn't show up

| Symptom | Cause |
|---|---|
| Not in the **Installed** list at all | The zip had the wrong shape, or the folder name does not match the package. Rebuild with `tools/package.py`. |
| Listed but will not enable | Something raised on import. *Plugins → Python Console* shows the traceback, as does the *Log Messages* panel. |
| Visible but **Extract** complains about Earth Engine | Expected — install the [Google Earth Engine][ee-plugin] plugin. Browsing the catalog works without it. |
| Installed after a previous version | Restart QGIS, or use [Plugin Reloader][reloader] to reload it in place. |

[reloader]: https://plugins.qgis.org/plugins/plugin_reloader/

Before installing into a QGIS you have not tried before, these two answer "will it
even work here" without touching your profile:

```bash
python tools/api_audit.py    # does every QGIS/Qt symbol this plugin uses exist?
python tools/smoke_test.py   # does it load, and does the dock actually work?
```

Both need a QGIS Python. On Windows with OSGeo4W that is
`C:\OSGeo4W\bin\python-qgis-ltr.bat tools\api_audit.py`.

### Requirements

| | |
|---|---|
| QGIS | 3.22 or newer, **including 4.x** — one code base, [verified on both Qt majors](#tested-against) |
| For browsing | nothing beyond QGIS |
| For extracting | the [**Google Earth Engine**][ee-plugin] plugin, which provides the `ee` Python API and handles authentication |
| For real H3 indexes | the optional [`h3`][h3] package — without it, tiling falls back to an equivalent lat/lon grid |

[ee-plugin]: https://plugins.qgis.org/plugins/ee_plugin/

You also need a Google account [registered for Earth Engine][register]. Recent
Earth Engine API versions require a Cloud project as well; the Google Earth
Engine plugin walks you through both.

[register]: https://code.earthengine.google.com/register

<details>
<summary>If Earth Engine will not sign in</summary>

Symptom: the Google Earth Engine plugin fails to load with *"Please authorize
access to your Earth Engine account"*, or this plugin reports **Earth Engine is
not signed in**. Because that plugin fails inside its own `classFactory`, it never
finishes loading, so its sign-in command is unavailable and you have to
authenticate around it.

From the QGIS Python Console (*Plugins → Python Console*) — this works even with
the plugin broken, since its bundled `ee` is already on the path:

```python
import ee

ee.Authenticate()
```

A browser opens; if it does not, use `ee.Authenticate(auth_mode='notebook')`, which
prints a URL and takes a pasted code.

**Then restart QGIS, and expect to be asked for your Cloud project again.**
`ee.Authenticate()` writes `~/.config/earthengine/credentials` from scratch rather
than merging, and the Earth Engine plugin stores the project id *in that same
file* — so authenticating clears it. Your project is shown in the
[Code Editor](https://code.earthengine.google.com), and it must be
[registered for Earth Engine][register].

A credentials file more than a year or two old has usually been revoked, and no
amount of retrying will help; re-authenticating is the fix.

</details>

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

## Auto-scaling large areas

Earth Engine answers at most **1,048,576 values** per `getRegion` call, where
values = points x bands x images. A US state at Sentinel-5P's native 1,113 m, over
a year, across 12 bands, is about a thousand times that. So the plugin tiles it.

### Picking the tiling

The resolution is **calculated, not probed**. Point count scales as the inverse
square of the ground resolution, so the area one request can cover is:

```
area_budget_km² = limit × 0.7 × (resolution_m / 1000)² / (bands × images)
```

The search then walks H3 resolutions from coarse to fine and takes the first whose
cells fit in that budget. Coarse wins, because each tile is a round trip.

| H3 res | Cell area | Scale |
|---:|---:|---|
| 3 | ~12,393 km² | a region or large state |
| 4 | ~1,770 km² | a county |
| 5 | ~253 km² | a metropolitan area |
| 6 | ~36 km² | a city |
| 7 | ~5.2 km² | a town |
| 8 | ~0.74 km² | a neighbourhood |

Pennsylvania (139,000 km²), 3 bands, 1,113 m:

| Date range | Images | Tiling | Tiles |
|---|---:|---|---:|
| 1 day | 1 | not needed | 1 |
| 1 month | 30 | res 4 | 84 |
| 1 year | 183 | res 5 | 545 |

Because it is arithmetic, planning is instant and costs no Earth Engine quota. The
dialog shows the tile count, tile size, per-tile estimate and predicted time before
anything is sent, and you can override the resolution either way.

### Fetching the tiles

Pick a worker count from 1 to half your CPUs (capped at 8 — past that Earth Engine
throttles anyway). The dialog shows the predicted time and speed-up for each:

```
sequential (one request at a time) — 18 min 10 s
2 parallel requests               — 11 min 21 s  (1.6×)
4 parallel requests               —  5 min 41 s  (3.2×)
8 parallel requests               —  2 min 50 s  (6.4×)
```

**Sequential is the default**: a little slower, but it cannot be rate limited for
concurrency and there is less to reason about when something goes wrong.

These are threads, not processes. An Earth Engine request is almost entirely an
HTTPS round trip and Python releases the GIL while waiting, so threads give the
full speed-up without re-importing QGIS per worker or pickling `ee` objects. The
CPU cap is kept because it is the intuitive dial, but the real ceiling is usually
Earth Engine's rate limit, not your CPU.

### Watching it run

A status panel appears under the results list while anything is running:

```
Extracting                                            [####------]  26%
Tile 143 of 545 · 28,600 records · 2.1 tiles/s · about 3 min 11 s left    [✕]
```

| State | What you see |
|---|---|
| Normal | position, records so far, throughput, remaining time |
| Rate limited | `rate limited, backing off` — rather than a bar that looks stuck |
| Tile split | `split into 7 smaller tiles`, and the denominator grows |
| Some tiles failed | a running `2 failed` count |
| One opaque request | a busy bar and ticking elapsed time (Earth Engine reports no progress for these) |
| Done | `109,000 records · done in 4 min 20 s` |

The remaining time is measured from actual throughput once a few tiles are in, so
it corrects itself rather than repeating a figure guessed before the job started.
**Cancel** is co-operative: requests already in flight still have to return, and
the panel says `Stopping…` rather than pretending otherwise.

### When Earth Engine pushes back

| Failure | Response |
|---|---|
| HTTP 429, quota, "too many concurrent" | Exponential backoff with jitter. Concurrency is narrowed for *every* worker, not just the one that saw it, then widened again after a run of successes. |
| "Too many values", "user memory limit exceeded" | Classified as *too large*, not transient — retrying unchanged would only burn attempts. The tile is split one resolution finer (7 children, area exactly conserved) and retried, up to 3 times. |
| Anything else | Reported with the tile id, not swallowed. |

So a throttled job slows down instead of failing.

### H3, and life without it

`h3` is a compiled extension and QGIS does not bundle it, so it is **optional**:

- **With `h3`** — tiles are real H3 cells and every extracted row carries
  `h3_index` and `tile_resolution`. Output joins directly against anything else
  keyed by H3, including `h3-js` on the web side.
- **Without `h3`** — tiles come from a latitude/longitude grid that covers the area
  just as exactly. Rows carry `tile_id` instead of `h3_index`, and the dialog says
  plainly that the ids are not real H3 indexes.

Nothing is installed automatically. The dialog shows the right command for your
platform; on OSGeo4W that is:

```
C:\OSGeo4W\bin\python-qgis-ltr.bat -m pip install h3
```

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

pytest tests              # 601 tests, no QGIS needed
pytest -m network         # also hit the live Earth Engine catalog
ruff check . && ruff format --check .

python tools/smoke_test.py       # load the plugin in a real QGIS and drive the dock
python tools/api_audit.py        # check every QGIS/Qt symbol exists on this build
python tools/refresh_catalog.py  # regenerate the bundled snapshot
python tools/package.py          # build the installable zip
```

CI runs the tests on Python 3.9 and 3.12, with and without `h3`, and runs the
smoke test and the API audit against QGIS 3.22, 3.44.15 LTR, 4.2.3 and master.

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
    hexgrid.py   H3 tiling, with a lat/lon grid fallback
    plan.py      auto-scale: choose a tiling, cost it, refine on refusal
    concurrency.py  worker policy, 429 backoff, the parallel runner
    progress.py  job statistics and the status wording
    earthengine.py  the only module that imports `ee`
  gui/         Qt and QGIS live here
    dockwidget.py   the dock, built in code (no .ui file)
    catalog_model.py  table model over the result set
    tasks.py        QgsTask subclasses for background work
    autoscale.py    the tiling plan dialog
    progress.py     the status panel widget
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

### Tested against

Every release is checked on real builds, not just the one on the developer's
machine:

| QGIS | Qt | PyQt | Python | API symbols | Smoke checks |
|---|---|---|---|---|---|
| 3.22.16 | 5.15.3 | 5.15.6 | 3.10.6 | 104/104 | 96/96 |
| 3.40.6 | 6.8.1 | 6.8.0 | 3.12.10 | 104/104 | 96/96 |
| 3.44.15 LTR | 5.15.13 | 5.15.11 | 3.12.14 | 104/104 | 96/96 |
| 4.2.3 | 6.11.0 | 6.11.0 | 3.12.14 | 104/104 | 96/96 |

Both Qt majors, both PyQt majors, Python 3.10 through 3.12 — with no conditional
code anywhere. To check any other build yourself:

```bash
python3 tools/api_audit.py     # does every QGIS/Qt symbol exist here?
python3 tools/smoke_test.py    # does it load, and does the dock work?
```

Or without installing anything, via the official QGIS images:

```bash
docker run --rm -v "$PWD:/src" -w /src qgis/qgis:ltr   sh -c "xvfb-run -a python3 tools/smoke_test.py"
```

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
