#!/usr/bin/env python3
"""Everything to check before uploading to plugins.qgis.org, as one command.

    python tools/preflight.py                  # build, then check the result
    python tools/preflight.py --zip dist/x.zip # check an existing zip

Checks the packaged artifact, not the working tree, because the zip is what
reviewers and users actually receive.  Exits non-zero if anything would fail the
upload or embarrass us afterwards.

Deliberately includes the boring ones -- a stale catalog, a machine path baked
into a file, a leftover credential -- because those are the mistakes that are
invisible in a diff and permanent once published.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import struct
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "ee_s5p_plugin"

#: plugins.qgis.org rejects an upload larger than this.
MAX_ZIP_MB = 25

#: Fields the plugin repository requires.
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

#: Anything matching these in a shipped file is a release-stopper.
SECRET_PATTERNS = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"\brefresh_token\b\s*[:=]\s*['\"][^'\"]{20,}", "a refresh token"),
    (r"\bclient_secret\b\s*[:=]\s*['\"][^'\"]{10,}", "a client secret"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "a Google API key"),
    (r"\bghp_[0-9A-Za-z]{30,}", "a GitHub token"),
    (r"\bxox[baprs]-[0-9A-Za-z-]{10,}", "a Slack token"),
    (r"password\s*[:=]\s*['\"][^'\"]{4,}", "a hard-coded password"),
)

#: A path from the machine this was built on should never ship.
MACHINE_PATHS = (r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", r"/home/[a-z0-9._-]+/")

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELLOW = RESET = ""


class Report:
    def __init__(self):
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passed = 0

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}pass{RESET}  {label}")
        else:
            self.failures.append(f"{label}{': ' + detail if detail else ''}")
            print(f"  {RED}FAIL{RESET}  {label}{': ' + detail if detail else ''}")
        return ok

    def warn(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  {GREEN}pass{RESET}  {label}")
        else:
            self.warnings.append(f"{label}{': ' + detail if detail else ''}")
            print(f"  {YELLOW}warn{RESET}  {label}{': ' + detail if detail else ''}")
        return ok

    def section(self, title: str) -> None:
        print(f"\n{title}")


# ---------------------------------------------------------------------------


def read_metadata(archive: zipfile.ZipFile) -> dict:
    raw = archive.read(f"{PACKAGE}/metadata.txt").decode("utf-8")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(raw)
    return dict(parser.items("general"))


def check_structure(archive: zipfile.ZipFile, report: Report) -> None:
    report.section("Package structure")
    names = archive.namelist()
    tops = {n.split("/")[0] for n in names}
    report.check(
        "exactly one top-level directory",
        tops == {PACKAGE},
        f"found {sorted(tops)}",
    )
    report.check("metadata.txt at the package root", f"{PACKAGE}/metadata.txt" in names)
    report.check("__init__.py present", f"{PACKAGE}/__init__.py" in names)
    report.check("LICENSE shipped", f"{PACKAGE}/LICENSE" in names)

    junk = [
        n
        for n in names
        if "__pycache__" in n
        or n.endswith((".pyc", ".pyo", ".orig", ".rej", ".bak"))
        or "/.git" in n
    ]
    report.check("no build or VCS junk", not junk, f"{junk[:4]}")

    leaked = [
        n
        for n in names
        if n.startswith((f"{PACKAGE}/tests/", f"{PACKAGE}/tools/"))
        or n.endswith(("pyproject.toml", "requirements-dev.txt"))
    ]
    report.check("no tests or tooling shipped", not leaked, f"{leaked[:4]}")


def check_size(path: str, archive: zipfile.ZipFile, report: Report) -> None:
    report.section("Size")
    mb = os.path.getsize(path) / (1024 * 1024)
    report.check(
        f"zip is {mb:.2f} MiB, under the {MAX_ZIP_MB} MiB limit", mb < MAX_ZIP_MB
    )
    biggest = sorted(archive.infolist(), key=lambda i: i.file_size, reverse=True)[:3]
    for info in biggest:
        print(f"        {info.file_size / 1024:>8,.0f} KiB  {info.filename}")


def check_metadata(metadata: dict, report: Report) -> None:
    report.section("metadata.txt")
    for field in REQUIRED_METADATA:
        value = (metadata.get(field) or "").strip()
        report.check(f"{field} is set", bool(value))

    placeholders = [
        f
        for f, v in metadata.items()
        if v.strip() in ("http://repo", "http://bugs", "http://homepage")
    ]
    report.check("no placeholder URLs left", not placeholders, f"{placeholders}")

    version = metadata.get("version", "")
    report.check(
        "version looks like a release",
        bool(re.fullmatch(r"\d+\.\d+(\.\d+)?", version)),
        version,
    )
    report.check(
        "not flagged experimental",
        metadata.get("experimental", "False").strip().lower() in ("false", "no", "0", ""),
        "experimental=True hides it from most users",
    )
    report.check(
        "not flagged deprecated",
        metadata.get("deprecated", "False").strip().lower() in ("false", "no", "0", ""),
    )
    report.check(
        "email looks like an address",
        bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", metadata.get("email", ""))),
        metadata.get("email", ""),
    )

    minimum = metadata.get("qgisMinimumVersion", "")
    maximum = metadata.get("qgisMaximumVersion", "").strip()
    report.check(
        "listed as QGIS 4 ready",
        maximum.startswith("4") or minimum.startswith("4"),
        f"qgisMaximumVersion={maximum or '(unset)'} -- needs 4.99 for the QGIS 4 list",
    )
    report.check(
        "supportsQt6 removed",
        "supportsQt6" not in metadata,
        "it was dropped from QGIS and is ignored",
    )
    report.warn("changelog present", bool(metadata.get("changelog", "").strip()))
    report.warn(
        "about is substantial",
        len(metadata.get("about", "")) > 200,
        f"{len(metadata.get('about', ''))} chars",
    )
    tags = [t.strip() for t in metadata.get("tags", "").split(",") if t.strip()]
    report.warn("has tags", len(tags) >= 3, f"{len(tags)} tags")


def check_icon(archive: zipfile.ZipFile, metadata: dict, report: Report) -> None:
    report.section("Icon")
    name = metadata.get("icon", "").strip()
    report.check("icon is named in metadata", bool(name))
    if not name:
        return
    entry = f"{PACKAGE}/{name}"
    if not report.check(f"{name} is in the package", entry in archive.namelist()):
        return
    data = archive.read(entry)
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    report.check(
        "icon is PNG or JPEG (SVG is not accepted here)",
        is_png or data[:3] == b"\xff\xd8\xff",
        "metadata.txt takes a web-friendly raster",
    )
    if is_png:
        width, height = struct.unpack(">II", data[16:24])
        report.check(
            f"icon is {width}x{height}, large enough to downscale cleanly",
            min(width, height) >= 64,
            "a small icon has to be upscaled for toolbars and looks pixelated",
        )
        report.warn(
            "icon has an alpha channel",
            data[25] in (4, 6),
            "an opaque background shows as a block on dark themes",
        )


def check_contents(archive: zipfile.ZipFile, report: Report) -> None:
    report.section("Shipped content")
    secrets, paths = [], []
    for name in archive.namelist():
        if name.endswith("/") or name.endswith((".png", ".gz", ".jpg", ".svg")):
            continue
        try:
            text = archive.read(name).decode("utf-8")
        except (UnicodeDecodeError, KeyError):
            continue
        for pattern, what in SECRET_PATTERNS:
            if re.search(pattern, text):
                secrets.append(f"{name}: {what}")
        for pattern in MACHINE_PATHS:
            for match in re.findall(pattern, text):
                paths.append(f"{name}: {match}")
    report.check("no credentials or keys in the package", not secrets, f"{secrets[:3]}")
    report.check("no build-machine paths baked in", not paths, f"{paths[:3]}")


def check_catalog(archive: zipfile.ZipFile, report: Report) -> None:
    report.section("Bundled catalog")
    import gzip

    entry = f"{PACKAGE}/data/gee_catalog.json.gz"
    if not report.check("catalog snapshot shipped", entry in archive.namelist()):
        return
    payload = json.loads(gzip.decompress(archive.read(entry)).decode("utf-8"))
    count = payload.get("count", 0)
    report.check(f"catalog holds {count:,} datasets", count > 900)

    from datetime import datetime, timezone

    generated = payload.get("generated", "")
    try:
        when = datetime.strptime(generated, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        report.check("catalog carries a timestamp", False, generated)
        return
    age = (datetime.now(timezone.utc) - when).days
    report.warn(
        f"catalog snapshot is {age} days old",
        age <= 30,
        "run tools/refresh_catalog.py before release",
    )
    ids = {d["id"] for d in payload["datasets"]}
    report.check(
        "the Sentinel-5P products are present",
        all(f"COPERNICUS/S5P/OFFL/L3_{p}" in ids for p in ("NO2", "CO", "O3", "CH4")),
    )


def check_loads_from_zip(path: str, report: Report) -> None:
    """Extract the zip and import it, the way QGIS will after installing."""
    report.section("Loading the packaged plugin")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(path) as archive:
            archive.extractall(tmp)
        probe = (
            f"import sys; sys.path.insert(0, r'{tmp}');"
            "import ee_s5p_plugin as p;"
            "from ee_s5p_plugin.core import store;"
            "c = store.Catalog.from_snapshot("
            "  store.read_snapshot(store.bundled_path()));"
            "print(p.__version__, len(c))"
        )
        try:
            out = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            report.check("extracted package imports", False, "timed out")
            return
        ok = out.returncode == 0
        report.check(
            "extracted package imports and reads its catalog",
            ok,
            (out.stderr.strip().splitlines() or [""])[-1],
        )
        if ok:
            print(f"        {out.stdout.strip()}  (version, datasets)")


def check_repo(report: Report) -> None:
    report.section("Repository")

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
        ).stdout.strip()

    dirty = git("status", "--porcelain")
    report.warn(
        "working tree is committed",
        not dirty,
        f"{len(dirty.splitlines())} uncommitted change(s) -- the upload is built "
        "from these, so commit them to keep the tag honest",
    )
    report.warn(
        "master branch untouched",
        git("rev-parse", "master") == git("rev-parse", "origin/master"),
        "master should still match origin",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", help="check this zip instead of building one")
    args = parser.parse_args(argv)

    path = args.zip
    if not path:
        sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
        import package as packager

        try:
            path = packager.build()
        except packager.PackagingError as error:
            print(error, file=sys.stderr)
            return 1
        print()

    print(f"Pre-upload check: {os.path.basename(path)}")
    report = Report()
    with zipfile.ZipFile(path) as archive:
        metadata = read_metadata(archive)
        check_structure(archive, report)
        check_size(path, archive, report)
        check_metadata(metadata, report)
        check_icon(archive, metadata, report)
        check_contents(archive, report)
        check_catalog(archive, report)
    check_loads_from_zip(path, report)
    check_repo(report)

    print("\n" + "=" * 70)
    if report.failures:
        print(f"{RED}NOT READY{RESET} -- {len(report.failures)} blocker(s):")
        for failure in report.failures:
            print(f"  - {failure}")
    else:
        print(f"{GREEN}READY TO UPLOAD{RESET} -- {report.passed} checks passed")
    if report.warnings:
        print(f"\n{len(report.warnings)} thing(s) worth a look:")
        for warning in report.warnings:
            print(f"  - {warning}")
    if not report.failures:
        print(f"\n  Upload {path}")
        print("  at https://plugins.qgis.org/plugins/add/")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
