# Contributing

## Getting set up

```bash
git clone https://github.com/blcrosbie/ee_s5p_plugin
cd ee_s5p_plugin
pip install -r requirements-dev.txt
pytest tests
```

The test suite needs no QGIS install: everything in `ee_s5p_plugin/core` is free
of Qt, QGIS and `ee` imports, which is what makes it testable on plain Python.
That property is enforced by `tests/test_qt_compat.py`, so please keep it.

To run the plugin from a checkout, symlink it into your QGIS profile:

```bash
# Linux / macOS
ln -s "$PWD/ee_s5p_plugin" \
  ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/ee_s5p_plugin
```

```powershell
# Windows (run as administrator)
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ee_s5p_plugin" `
  -Target "$PWD\ee_s5p_plugin"
```

Then enable *Earth Engine Catalog Query* in *Plugins → Manage and Install
Plugins*. The [Plugin Reloader][reloader] plugin saves a lot of restarts.

[reloader]: https://plugins.qgis.org/plugins/plugin_reloader/

## Before opening a pull request

```bash
pytest tests                   # unit tests
ruff check . && ruff format .  # lint and format
python tools/smoke_test.py     # load in a real QGIS (needs a QGIS Python)
```

`pytest -m network` additionally crawls the live Earth Engine catalog. Run it
when you touch `core/stac.py` — it is what notices Google changing the catalog's
shape.

## Verifying on a new QGIS

Two tools, neither of which needs the plugin installed into a profile:

```bash
python3 tools/api_audit.py    # does every QGIS/Qt symbol still exist here?
python3 tools/smoke_test.py   # does the plugin load and does the dock work?
```

Run both against the QGIS you care about. On Windows with OSGeo4W:

```
C:\OSGeo4W\bin\python-qgis-ltr.bat tools\api_audit.py
C:\OSGeo4W\bin\python-qgis-ltr.bat tools\smoke_test.py
```

`api_audit.py` parses the sources for every name imported from `qgis.*` and every
dotted constant read off those names, then resolves each one against the running
QGIS. That is where version breakage actually shows up -- a removed class, or an
enum member that moved namespace. It cannot see methods called on instances
(`layer.triggerRepaint()`); the smoke test covers those by driving real widgets.

CI runs both across QGIS 3.22, 3.44.15 LTR, 4.2.3, the moving `ltr`/`stable`
channels, and master.

### QGIS 4

QGIS 4 is the Qt6 transition, and it kept deprecated APIs where it could, so the
rules in the next section are the whole story for this plugin. Two metadata facts,
from [the official guide](https://plugins.qgis.org/docs/migrate-qgis4):

- `qgisMaximumVersion=4.99` is what puts a 3.x-floor plugin in the **QGIS 4 Ready**
  list. Without it the plugin is hidden from QGIS 4 users.
- `supportsQt6` has been **removed** from QGIS and is no longer recognised. Do not
  add it back; a test asserts it is absent.

## The two rules that matter

### 1. `core` must not import Qt, QGIS or `ee` at module level

`core` holds the catalog, filtering, date handling, geometry, request sizing and
export logic. Keeping it dependency-free is what lets it be tested quickly and
what lets the catalog browser work when Earth Engine is not installed. `qgis` and
`ee` may be imported *inside a function*, as a guarded lazy import — see
`core/earthengine.require_ee` and `core/store.cache_dir`.

### 2. Qt enums must be written in scoped form

The plugin supports QGIS 3.22 (Qt 5.15) through QGIS 4.x (Qt 6). No compatibility
shim is needed, because PyQt 5.15 accepts the fully scoped spelling that PyQt 6
requires — but only if every call site uses it.

| Don't | Do |
|---|---|
| `Qt.Checked` | `Qt.CheckState.Checked` |
| `Qt.CheckStateRole` | `Qt.ItemDataRole.CheckStateRole` |
| `QMessageBox.Ok` | `QMessageBox.StandardButton.Ok` |
| `QMessageBox.Information` | `QMessageBox.Icon.Information` |
| `QHeaderView.Stretch` | `QHeaderView.ResizeMode.Stretch` |
| `widget.exec_()` | `widget.exec()` |
| `QRegExp` | `QRegularExpression` |
| `from PyQt5 import …` | `from qgis.PyQt import …` |

`tests/test_qt_compat.py` checks all of this statically. If you need a new enum,
add it to the lists there as well.

## Conventions

- **Exceptions**: no bare `except:`. Catch the specific error; `except Exception`
  is fine where one bad record must not abort a whole crawl or user action, but
  log what was swallowed. A test forbids bare excepts.
- **User-facing text**: wrap it in `self.tr(...)` and make the message say what to
  do next, not just what went wrong.
- **Dialogs**: use the helpers in `gui/messages.py`. Prefer `push_*` (message bar)
  over a modal dialog; a modal is for when you actually need an answer.
- **Background work**: anything that touches the network or Earth Engine belongs in
  a `QgsTask` in `gui/tasks.py`. `run()` executes off the GUI thread and must not
  touch widgets; hand results back through `finished()`.
- **Comments** explain *why*, especially where the code works around something
  Earth Engine or Qt does. Several comments record bugs from the 2020 version so
  they are not reintroduced — please keep that habit.

## Working on auto-scale

The tiling logic lives in three `core` modules, all free of Qt and `ee`:

| Module | Responsibility |
|---|---|
| `hexgrid.py` | Cut an area into tiles. `H3Grid` when `h3` is installed, `LatLonGrid` otherwise. Also the H3 area table and resolution choice. |
| `plan.py` | Decide *which* resolution, cost the plan, and subdivide a refused tile. |
| `concurrency.py` | Worker policy, failure classification, backoff, the job runner. |

Two things to keep in mind:

1. **`h3` is optional and must stay that way.** QGIS does not bundle it. Anything
   that only works with `h3` needs a fallback and a message saying what is
   degraded. Tests that need the real library use the `needs_h3` skip marker; tests
   for the fallback use the `no_h3` fixture, which forces it either way. Run both.
2. **Never auto-install it.** `hexgrid.install_hint()` returns advice for the
   user's platform. The 2020 version shelled out to `pip install beautifulsoup4`
   from inside the plugin; that can break someone's QGIS and must not come back.

If you change the resolution arithmetic, the test that matters is
`test_plan.py::TestAutoScale::test_every_planned_tile_fits_within_the_limit` — it
asserts that every tile a plan produces actually passes the size estimator, which
is the whole promise of the feature.

## Adding a new dataset type

If Google introduces a `gee:type` the plugin does not handle, the opt-in test
`test_every_gee_type_in_the_catalog_is_accounted_for` fails. Either add it to
`stac.EE_CLASS_BY_GEE_TYPE` with the `ee` class that opens it, or, if it cannot be
opened through the Python API, add it to `known_unsupported` in that test with a
comment saying why.

## Releasing

1. Update the version in **both** `ee_s5p_plugin/metadata.txt` and
   `ee_s5p_plugin/__init__.py` — `tools/package.py --validate` fails if they
   disagree.
2. Add a `CHANGELOG.md` entry and mirror the summary into the `changelog=` field
   in `metadata.txt`.
3. Refresh the bundled catalog: `python tools/refresh_catalog.py`.
4. `python tools/package.py` and check the zip.
5. Tag it: `git tag v1.0.1 && git push --tags`. CI builds the zip, attaches it to
   a GitHub release, and uploads it to plugins.qgis.org if `OSGEO_USERNAME` and
   `OSGEO_PASSWORD` are set in the repository secrets.

The **first** upload of a new plugin has to go through the web form at
<https://plugins.qgis.org/plugins/add/>; the XML-RPC endpoint only accepts new
versions of a plugin that already exists.

## A note on `master`

The `master` branch holds the original 2020 implementation, written for a
master's degree. It is kept unchanged as a record of that work — please don't
send pull requests against it. All development happens on `main`.
