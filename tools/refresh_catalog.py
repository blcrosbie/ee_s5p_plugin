#!/usr/bin/env python3
"""Regenerate the catalog snapshot that ships inside the plugin.

    python tools/refresh_catalog.py              # update the bundled snapshot
    python tools/refresh_catalog.py --check      # report drift, write nothing
    python tools/refresh_catalog.py -o out.json  # write somewhere else

``--check`` exits 1 when the live catalog differs from the bundled snapshot,
which is what the scheduled CI job uses to decide whether to open a commit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "ee_s5p_plugin"))

from core import stac, store


def _progress(quiet: bool):
    state = {"last": 0.0}

    def report(done: int, total: int, message: str) -> bool:
        if quiet:
            return True
        now = time.monotonic()
        if now - state["last"] < 0.25 and done != total:
            return True
        state["last"] = now
        print(f"\r  {message:<60}", end="", flush=True)
        return True

    return report


def _summarise(datasets, skipped) -> None:
    print(f"\n  datasets        : {len(datasets)}")
    for name, count in stac.summarise(datasets).items():
        print(f"    {name:<18}: {count}")
    print(f"  providers       : {len({d['provider'] for d in datasets})}")
    print(f"  distinct tags   : {len({t for d in datasets for t in d.get('tags', ())})}")
    print(f"  deprecated      : {sum(1 for d in datasets if d.get('deprecated'))}")
    print(f"  still updating  : {sum(1 for d in datasets if d.get('ongoing'))}")
    if skipped:
        print("  skipped records :")
        for reason, count in sorted(skipped.items()):
            note = (
                " (not reachable through the Earth Engine Python API)"
                if reason == "bigquery_table"
                else ""
            )
            print(f"    {reason:<18}: {count}{note}")


def _dataset_ids(snapshot: dict) -> set[str]:
    return {d["id"] for d in snapshot.get("datasets") or () if d.get("id")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o",
        "--output",
        help="where to write (default: the plugin's bundled snapshot)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the bundled snapshot and exit 1 if it is out of date",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=stac.DEFAULT_WORKERS,
        help=f"parallel requests (default {stac.DEFAULT_WORKERS})",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    destination = args.output or store.bundled_path()
    if not args.quiet:
        print(f"Crawling {stac.ROOT_CATALOG_URL}")

    started = time.monotonic()
    skipped: dict[str, int] = {}
    try:
        datasets = stac.fetch_datasets(
            workers=args.workers,
            progress=_progress(args.quiet),
            skipped=skipped,
        )
    except stac.StacError as error:
        print(f"\nFailed: {error}", file=sys.stderr)
        return 2

    if not datasets:
        print("\nFailed: the catalog returned no datasets", file=sys.stderr)
        return 2

    snapshot = stac.build_snapshot(datasets)
    if not args.quiet:
        _summarise(snapshot["datasets"], skipped)
        print(f"  elapsed         : {time.monotonic() - started:.1f}s")

    if args.check:
        try:
            current = store.read_snapshot(store.bundled_path())
        except store.SnapshotError as error:
            print(f"\nBundled snapshot unusable ({error}); refresh needed.")
            return 1
        live_ids, bundled_ids = _dataset_ids(snapshot), _dataset_ids(current)
        added, removed = live_ids - bundled_ids, bundled_ids - live_ids
        # Compare the records too, so changed dates and new bands are caught.
        changed = sum(
            1
            for live in snapshot["datasets"]
            for old in (
                {d["id"]: d for d in current.get("datasets") or ()}.get(live["id"]),
            )
            if old is not None and _comparable(old) != _comparable(live)
        )
        print()
        print(f"  added   : {len(added)}")
        print(f"  removed : {len(removed)}")
        print(f"  changed : {changed}")
        for dataset_id in sorted(added)[:20]:
            print(f"    + {dataset_id}")
        for dataset_id in sorted(removed)[:20]:
            print(f"    - {dataset_id}")
        if added or removed or changed:
            print("\nThe bundled snapshot is out of date.")
            return 1
        print("\nThe bundled snapshot is up to date.")
        return 0

    path = store.write_snapshot(snapshot, destination)
    size_kb = os.path.getsize(path) / 1024.0
    if not args.quiet:
        print(f"\nWrote {path} ({size_kb:,.0f} KiB)")
    return 0


def _comparable(dataset: dict) -> str:
    """A dataset record with volatile fields removed, for drift comparison."""
    ignore = {"ongoing"}
    return json.dumps(
        {k: v for k, v in dataset.items() if k not in ignore}, sort_keys=True
    )


if __name__ == "__main__":
    raise SystemExit(main())
