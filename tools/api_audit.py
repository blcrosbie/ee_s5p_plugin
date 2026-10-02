#!/usr/bin/env python3
"""Check every QGIS and Qt symbol this plugin uses against a running QGIS.

The smoke test proves the plugin *works* on one QGIS build.  This answers a
different question: does every API it touches still exist on *this* build?  That
is the question when a new QGIS comes out, because QGIS 4 kept deprecated APIs
"where possible" rather than entirely, and scoped enum members are exactly the
kind of thing that moves.

    python3 tools/api_audit.py                 # audit against the running QGIS
    python3 tools/api_audit.py --list          # just print what would be checked
    python3 tools/api_audit.py --json report.json

On Windows with OSGeo4W::

    C:\\OSGeo4W\\bin\\python-qgis-ltr-qt6.bat tools\\api_audit.py

Exits non-zero if anything is missing, so it works as a CI gate.

What it cannot see: methods called on instances (``layer.triggerRepaint()``),
because that needs a live object -- the smoke test covers those.  What it does
see is every imported class and every dotted constant, which is where version
breakage actually shows up.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(REPO_ROOT, "ee_s5p_plugin")

#: Import roots worth auditing.  ``qgis.PyQt`` is QGIS's own Qt shim, so it covers
#: both Qt majors.
AUDITED_ROOTS = ("qgis",)


def python_files() -> list[str]:
    found = []
    for root, dirs, files in os.walk(PACKAGE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        found.extend(
            os.path.join(root, name) for name in sorted(files) if name.endswith(".py")
        )
    return sorted(found)


def _audited(module: str) -> bool:
    return module.split(".")[0] in AUDITED_ROOTS


def collect(paths: list[str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(imports, attributes)``.

    ``imports`` maps a module to the names imported from it.  ``attributes`` maps
    an imported name to the dotted paths read off it, e.g. ``Qt`` ->
    ``{"ItemDataRole.UserRole", "CheckState.Checked"}``.
    """
    imports: dict[str, set[str]] = defaultdict(set)
    attributes: dict[str, set[str]] = defaultdict(set)

    for path in paths:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        # Which local names came from an audited module?
        local: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and _audited(node.module):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imports[node.module].add(alias.name)
                    local[alias.asname or alias.name] = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _audited(alias.name):
                        imports[alias.name].add("<module>")
                        local[alias.asname or alias.name] = alias.name

        # Dotted reads rooted at one of those names.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parts: list[str] = []
            cursor: ast.AST = node
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if not isinstance(cursor, ast.Name) or cursor.id not in local:
                continue
            parts.reverse()
            # Only constants are resolvable without an instance: a dotted path of
            # two or more parts, starting with a capital letter (an enum class).
            if len(parts) >= 2 and parts[0][:1].isupper():
                attributes[cursor.id].add(".".join(parts))

    return dict(imports), dict(attributes)


def resolve(dotted: str, obj: object) -> tuple[bool, str]:
    cursor = obj
    for part in dotted.split("."):
        try:
            cursor = getattr(cursor, part)
        except AttributeError:
            return False, part
    return True, ""


def audit(imports: dict[str, set[str]], attributes: dict[str, set[str]]) -> dict:
    """Resolve everything against the running QGIS."""
    import importlib

    report: dict = {"environment": {}, "missing": [], "checked": 0}

    try:
        from qgis.core import Qgis
        from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

        report["environment"] = {
            "qgis": Qgis.QGIS_VERSION,
            "qt": QT_VERSION_STR,
            "pyqt": PYQT_VERSION_STR,
            "python": sys.version.split()[0],
        }
    except ImportError as error:
        report["environment"] = {"error": str(error)}
        return report

    resolved: dict[str, object] = {}

    for module_name, names in sorted(imports.items()):
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            report["missing"].append(
                {"kind": "module", "symbol": module_name, "detail": str(error)}
            )
            continue
        for name in sorted(names):
            report["checked"] += 1
            if name == "<module>":
                resolved[module_name.split(".")[-1]] = module
                continue
            if not hasattr(module, name):
                # A submodule is not an attribute of its package until it has
                # been imported, so `from qgis.PyQt import sip` looks missing
                # until we actually try it.
                try:
                    resolved[name] = importlib.import_module(f"{module_name}.{name}")
                except ImportError:
                    report["missing"].append(
                        {"kind": "name", "symbol": f"{module_name}.{name}"}
                    )
                continue
            resolved[name] = getattr(module, name)

    for root, paths in sorted(attributes.items()):
        base = resolved.get(root)
        if base is None:
            continue
        for dotted in sorted(paths):
            report["checked"] += 1
            ok, failed_at = resolve(dotted, base)
            if not ok:
                report["missing"].append(
                    {
                        "kind": "attribute",
                        "symbol": f"{root}.{dotted}",
                        "detail": f"no attribute {failed_at!r}",
                    }
                )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list", action="store_true", help="print the symbols, check nothing"
    )
    parser.add_argument("--json", help="also write the report to this file")
    args = parser.parse_args(argv)

    imports, attributes = collect(python_files())

    if args.list:
        for module_name, names in sorted(imports.items()):
            for name in sorted(names):
                print(f"{module_name}.{name}")
        for root, paths in sorted(attributes.items()):
            for dotted in sorted(paths):
                print(f"{root}.{dotted}")
        return 0

    report = audit(imports, attributes)
    environment = report["environment"]
    if "error" in environment:
        print(f"Needs a QGIS Python environment: {environment['error']}", file=sys.stderr)
        return 3

    print(
        f"QGIS {environment['qgis']}  Qt {environment['qt']}  "
        f"PyQt {environment['pyqt']}  Python {environment['python']}"
    )
    print(f"Checked {report['checked']} QGIS/Qt symbols.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"Report written to {args.json}")

    if report["missing"]:
        print(f"\n{len(report['missing'])} MISSING on this build:")
        for entry in report["missing"]:
            detail = f" ({entry['detail']})" if entry.get("detail") else ""
            print(f"  {entry['kind']:<9} {entry['symbol']}{detail}")
        print(
            "\nEach of these needs a replacement that exists on every QGIS the "
            "plugin claims to support."
        )
        return 1

    print("\nAll symbols resolve on this build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
