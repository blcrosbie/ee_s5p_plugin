#!/usr/bin/env python3
"""Build the plugin zip that plugins.qgis.org accepts.

    python tools/package.py              # dist/ee_s5p_plugin-<version>.zip
    python tools/package.py --validate   # check metadata and contents only

The repository's plugin package must be the single top-level directory inside the
zip, with ``metadata.txt`` at its root -- the upload is rejected otherwise.  This
also refuses to build if the metadata version and ``__init__.__version__``
disagree, which is the mistake that ships a release labelled with the old number.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import os
import re
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_NAME = "ee_s5p_plugin"
PACKAGE_DIR = os.path.join(REPO_ROOT, PACKAGE_NAME)
DIST_DIR = os.path.join(REPO_ROOT, "dist")

#: Never shipped to users.
EXCLUDE_PATTERNS = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "*.ts",
    "*.orig",
    "*.rej",
    ".*",
    "*~",
)

#: Must be present in the zip for the plugin to work.
REQUIRED_FILES = (
    "metadata.txt",
    "__init__.py",
    "plugin.py",
    "icon.png",
    "LICENSE",
    "data/gee_catalog.json.gz",
    "core/catalog.py",
    "gui/dockwidget.py",
)

#: plugins.qgis.org requires these metadata fields.
REQUIRED_METADATA = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
    "tracker",
)

#: Upload limit on plugins.qgis.org.
MAX_ZIP_MB = 25


class PackagingError(RuntimeError):
    pass


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_PATTERNS)


def read_metadata() -> dict[str, str]:
    path = os.path.join(PACKAGE_DIR, "metadata.txt")
    if not os.path.exists(path):
        raise PackagingError(f"No metadata.txt at {path}")
    parser = configparser.ConfigParser(interpolation=None)
    # QGIS metadata keys are camelCase; configparser lower-cases them unless told
    # otherwise, and silently returning None for 'qgisMinimumVersion' is worse
    # than keeping the spelling the file uses.
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    if not parser.has_section("general"):
        raise PackagingError("metadata.txt has no [general] section")
    return dict(parser.items("general"))


def read_init_version() -> str:
    path = os.path.join(PACKAGE_DIR, "__init__.py")
    with open(path, encoding="utf-8") as handle:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', handle.read(), re.M)
    if not match:
        raise PackagingError("No __version__ in __init__.py")
    return match.group(1)


def validate() -> dict[str, str]:
    """Check everything plugins.qgis.org and QGIS will check, before uploading."""
    problems: list[str] = []
    metadata = read_metadata()

    for field in REQUIRED_METADATA:
        value = (metadata.get(field) or "").strip()
        if not value:
            problems.append(f"metadata.txt: '{field}' is missing or empty")
        elif value.startswith("http://repo") or value in (
            "http://bugs",
            "http://homepage",
        ):
            problems.append(f"metadata.txt: '{field}' is still a placeholder ({value})")

    version = metadata.get("version", "")
    init_version = read_init_version()
    if version != init_version:
        problems.append(
            f"version mismatch: metadata.txt says {version!r}, "
            f"__init__.py says {init_version!r}"
        )
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", version):
        problems.append(f"version {version!r} should look like 1.0.0")

    # A plugin that does not declare a Qt6 maximum is hidden from QGIS 4 users.
    if metadata.get("supportsQt6", "").strip().lower() not in ("true", "yes", "1"):
        problems.append("metadata.txt: supportsQt6=True is required for QGIS 4")
    maximum = metadata.get("qgisMaximumVersion", "").strip()
    if maximum and not maximum.startswith("4"):
        problems.append(
            f"metadata.txt: qgisMaximumVersion={maximum} excludes QGIS 4; use 4.99"
        )

    for relative in REQUIRED_FILES:
        if not os.path.exists(os.path.join(PACKAGE_DIR, relative)):
            problems.append(f"missing required file: {relative}")

    # The bundled catalog must be real, not an empty placeholder.
    snapshot = os.path.join(PACKAGE_DIR, "data", "gee_catalog.json.gz")
    if os.path.exists(snapshot) and os.path.getsize(snapshot) < 100_000:
        problems.append(
            f"data/gee_catalog.json.gz is only {os.path.getsize(snapshot)} bytes; "
            "run tools/refresh_catalog.py"
        )

    if problems:
        raise PackagingError("\n  ".join(["Validation failed:", *problems]))
    return metadata


def collect_files() -> list[tuple[str, str]]:
    """``(absolute path, path inside the zip)`` for everything to ship."""
    entries = []
    for root, dirs, files in os.walk(PACKAGE_DIR):
        dirs[:] = sorted(d for d in dirs if not _excluded(d))
        for name in sorted(files):
            if _excluded(name):
                continue
            absolute = os.path.join(root, name)
            relative = os.path.relpath(absolute, REPO_ROOT).replace(os.sep, "/")
            entries.append((absolute, relative))
    return entries


def build(output: str | None = None) -> str:
    metadata = validate()
    version = metadata["version"]
    os.makedirs(DIST_DIR, exist_ok=True)
    target = output or os.path.join(DIST_DIR, f"{PACKAGE_NAME}-{version}.zip")

    entries = collect_files()
    # ZIP_DEFLATED and a fixed order keep the artifact reproducible.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for absolute, relative in entries:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(absolute, "rb") as handle:
                archive.writestr(info, handle.read())

    size_mb = os.path.getsize(target) / (1024 * 1024)
    if size_mb > MAX_ZIP_MB:
        raise PackagingError(
            f"{target} is {size_mb:.1f} MiB, over the {MAX_ZIP_MB} MiB "
            f"plugins.qgis.org limit"
        )

    print(f"Built {target}")
    print(f"  files   : {len(entries)}")
    print(f"  size    : {size_mb:.2f} MiB")
    print(f"  version : {version}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="zip path to write")
    parser.add_argument(
        "--validate", action="store_true", help="validate only, build nothing"
    )
    args = parser.parse_args(argv)

    try:
        if args.validate:
            metadata = validate()
            print(f"OK: {metadata['name']} {metadata['version']} is ready to package")
            return 0
        build(args.output)
    except PackagingError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
