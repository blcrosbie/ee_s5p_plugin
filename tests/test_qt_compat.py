"""Static guard on the Qt 5 / Qt 6 rules, so the port cannot silently regress.

The plugin has to load on QGIS 3.22 (Qt 5.15) through QGIS 4.x (Qt 6).  No
compatibility shim is needed, because PyQt 5.15 accepts the fully scoped enum
spelling that PyQt 6 *requires* -- but only if every call site uses it.  These
checks were derived by probing both environments, and each one corresponds to an
API that was verified to exist in Qt 5.15 and in Qt 6.8:

===========================  ==============================  ===============
Do not use                   Use instead                     Why
===========================  ==============================  ===============
``Qt.Checked``               ``Qt.CheckState.Checked``       Qt 6 scoped enums
``QMessageBox.Ok``           ``QMessageBox.StandardButton``  Qt 6 scoped enums
``widget.exec_()``           ``widget.exec()``               removed in PyQt6
``QRegExp``                  ``QRegularExpression``          removed in Qt 6
``setFilterRegExp``          ``setFilterRegularExpression``  removed in Qt 6
``from PyQt5 import ...``    ``from qgis.PyQt import ...``   binds one Qt major
``import resources``         load the icon from a file       pyrcc is gone
===========================  ==============================  ===============
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize

import pytest


def code_tokens(path: str) -> list[tuple[int, str]]:
    """``(line number, token text)`` for real code only.

    Docstrings, strings and comments are skipped: this file documents the banned
    spellings in its own docstring, and a naive text scan flags that as a
    violation.
    """
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    kept = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (
                tokenize.STRING,
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            ):
                continue
            kept.append((token.start[0], token.line))
    except tokenize.TokenError:  # pragma: no cover -- syntax is checked separately
        return [(n, line) for n, line in enumerate(source.splitlines(), start=1)]
    # One entry per line of code, de-duplicated, preserving order.
    seen = set()
    lines = []
    for number, line in kept:
        if number not in seen:
            seen.add(number)
            lines.append((number, line))
    return lines


PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ee_s5p_plugin"
)


def _python_files() -> list[str]:
    found = []
    for root, _dirs, files in os.walk(PLUGIN_DIR):
        if "__pycache__" in root:
            continue
        found.extend(os.path.join(root, name) for name in files if name.endswith(".py"))
    return sorted(found)


SOURCES = _python_files()


def _relative(path: str) -> str:
    return os.path.relpath(path, os.path.dirname(PLUGIN_DIR)).replace("\\", "/")


def test_there_are_sources_to_check():
    assert SOURCES, "no plugin sources found"


@pytest.mark.parametrize("path", SOURCES, ids=_relative)
def test_every_source_file_parses(path):
    with open(path, encoding="utf-8") as handle:
        ast.parse(handle.read(), filename=path)


@pytest.mark.parametrize("path", SOURCES, ids=_relative)
def test_no_direct_pyqt_imports(path):
    """Importing PyQt5/PyQt6 directly pins the plugin to one Qt major version."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in ("PyQt5", "PyQt6"):
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("PyQt5", "PyQt6"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, (
        f"{_relative(path)} imports PyQt directly; use qgis.PyQt instead:\n  "
        + "\n  ".join(offenders)
    )


#: ``pattern -> what to use instead``.
BANNED_PATTERNS = {
    r"\.exec_\(": "exec() -- exec_() was removed in PyQt6",
    r"\bQRegExp\b": "QRegularExpression -- QRegExp was removed in Qt6",
    r"\bsetFilterRegExp\b": "setFilterRegularExpression",
    r"\bpyqtSlot\b.*\bQString\b": "plain str -- QString is not exposed in PyQt5/6",
    r"\bQDesktopWidget\b": "QScreen -- QDesktopWidget was removed in Qt6",
    r"\bQtWidgets\.QApplication\.desktop\b": "QGuiApplication.screens()",
    r"\bfrom\s+\.resources\b": "load the icon from a file path -- pyrcc is gone",
    r"\bQVariant\(": "plain Python values -- QVariant wrapping is unnecessary",
    # Renamed or removed between Qt5 and Qt6.
    r"\bQFontMetrics[A-Za-z]*\.width\(": "horizontalAdvance()",
    r"\.setMargin\(": "setContentsMargins()",
    r"\bQPalette\.(Background|Foreground)\b": "QPalette.ColorRole.Window / WindowText",
    r"\bsetResizeMode\(": "setSectionResizeMode()",
    r"\bQt\.MidButton\b": "Qt.MouseButton.MiddleButton",
    r"\.setCodec\(": "QTextStream.setEncoding()",
    r"\bQLibraryInfo\.location\b": "QLibraryInfo.path()",
    r"\bqsrand\b|\bqrand\b": "the random module",
    r"\bQRegExpValidator\b": "QRegularExpressionValidator",
    r"\bQStringList\b": "a plain list of str",
    # sip is not re-exported by every QGIS build, and the guard silently returned
    # False where it is missing; track lifetime with Qt signals instead.
    r"\bsip\.isdeleted\b": "the taskCompleted / taskTerminated signals",
}


@pytest.mark.parametrize("path", SOURCES, ids=_relative)
def test_no_qt6_incompatible_apis(path):
    offenders = []
    for number, line in code_tokens(path):
        for pattern, replacement in BANNED_PATTERNS.items():
            if re.search(pattern, line):
                offenders.append(f"line {number}: {line.strip()} -> use {replacement}")
    assert not offenders, f"{_relative(path)}:\n  " + "\n  ".join(offenders)


#: Enum members that exist unscoped only on Qt 5.  Writing ``Qt.Checked`` raises
#: AttributeError on Qt 6, and these are the ones this plugin actually touches.
UNSCOPED_QT_ENUMS = (
    "Checked",
    "Unchecked",
    "PartiallyChecked",
    "CheckStateRole",
    "DisplayRole",
    "ToolTipRole",
    "UserRole",
    "FontRole",
    "TextAlignmentRole",
    "ItemIsUserCheckable",
    "ItemIsEnabled",
    "ItemIsSelectable",
    "LeftDockWidgetArea",
    "RightDockWidgetArea",
    "AlignRight",
    "AlignLeft",
    "AlignVCenter",
    "Horizontal",
    "Vertical",
    "AscendingOrder",
    "DescendingOrder",
    "CaseInsensitive",
    "Key_Delete",
    "Key_Return",
)


@pytest.mark.parametrize("path", SOURCES, ids=_relative)
def test_qt_enums_are_scoped(path):
    """``Qt.Checked`` must be written ``Qt.CheckState.Checked``."""
    offenders = []
    for number, line in code_tokens(path):
        for member in UNSCOPED_QT_ENUMS:
            if re.search(rf"\bQt\.{member}\b", line):
                offenders.append(f"line {number}: Qt.{member}")
    assert not offenders, (
        f"{_relative(path)} uses unscoped Qt enums, which fail on Qt6:\n  "
        + "\n  ".join(offenders)
    )


#: Widget classes whose enums were also unscoped on Qt 5.
UNSCOPED_WIDGET_ENUMS = {
    "QMessageBox": (
        "Ok",
        "Cancel",
        "Yes",
        "No",
        "Close",
        "Information",
        "Warning",
        "Critical",
        "Question",
    ),
    "QHeaderView": ("Stretch", "ResizeToContents", "Fixed", "Interactive"),
    "QAbstractItemView": (
        "ExtendedSelection",
        "SingleSelection",
        "SelectRows",
        "NoEditTriggers",
        "NoSelection",
    ),
    "QSizePolicy": ("Expanding", "Preferred", "Fixed", "Minimum", "Maximum"),
    "QDialogButtonBox": ("Ok", "Cancel", "Close", "Apply"),
    "QCompleter": ("PopupCompletion", "InlineCompletion", "UnfilteredPopupCompletion"),
    "QFileDialog": ("AcceptSave", "AcceptOpen", "Directory"),
    "QgsTask": ("CanCancel", "CancelWithoutPrompt", "Hidden"),
}


@pytest.mark.parametrize("path", SOURCES, ids=_relative)
def test_widget_enums_are_scoped(path):
    offenders = []
    for number, line in code_tokens(path):
        for cls, members in UNSCOPED_WIDGET_ENUMS.items():
            for member in members:
                if re.search(rf"\b{cls}\.{member}\b", line):
                    offenders.append(f"line {number}: {cls}.{member}")
    assert not offenders, (
        f"{_relative(path)} uses unscoped widget enums, which fail on Qt6:\n  "
        + "\n  ".join(offenders)
    )


def test_core_package_is_free_of_qt_and_earth_engine():
    """``core`` must stay importable, and testable, without Qt or ``ee``.

    This is what lets the catalog, filtering, sizing and export logic be tested
    on plain Python -- and what keeps a missing Earth Engine install from
    breaking catalog browsing.
    """
    core_dir = os.path.join(PLUGIN_DIR, "core")
    offenders = []
    for name in sorted(os.listdir(core_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(core_dir, name)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                root = module.split(".")[0]
                if root in ("PyQt5", "PyQt6"):
                    offenders.append(f"{name}:{node.lineno} imports {module}")
                # ee and qgis may be imported only inside a function, as a lazy
                # guarded import -- never at module level.
                if root in ("ee", "qgis") and _at_module_level(tree, node):
                    offenders.append(
                        f"{name}:{node.lineno} imports {module} at module level"
                    )
    assert not offenders, "core must not depend on Qt/ee:\n  " + "\n  ".join(offenders)


def _at_module_level(tree: ast.Module, target: ast.AST) -> bool:
    return any(node is target for node in tree.body)


def _metadata() -> dict:
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(os.path.join(PLUGIN_DIR, "metadata.txt"), encoding="utf-8")
    return dict(parser.items("general"))


class TestMetadataTargetsQgis4:
    """plugins.qgis.org decides QGIS 4 readiness from the version range.

    See https://plugins.qgis.org/docs/migrate-qgis4 -- a plugin is listed as QGIS 4
    ready when qgisMinimumVersion >= 4.0 or qgisMaximumVersion >= 4.0.
    """

    def test_the_version_ceiling_reaches_qgis_4(self):
        metadata = _metadata()
        minimum = metadata.get("qgisMinimumVersion", "")
        maximum = metadata.get("qgisMaximumVersion", "")
        assert maximum.startswith("4") or minimum.startswith("4"), (
            f"qgisMinimumVersion={minimum} qgisMaximumVersion={maximum} would keep "
            "this plugin out of the QGIS 4 Ready list"
        )

    def test_supports_qt6_is_absent(self):
        """It was removed from QGIS core and is no longer recognised.

        Plugins that relied on it alone were dropped from the QGIS 4 list, so
        carrying it is at best dead weight.
        """
        assert "supportsQt6" not in _metadata()


def test_no_bare_except_clauses():
    """``except:`` swallows KeyboardInterrupt and hides real bugs.

    v0.1 had dozens, which is why failures surfaced as an empty result list with
    no explanation.
    """
    offenders = []
    for path in SOURCES:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{_relative(path)}:{node.lineno}")
    assert not offenders, "bare 'except:' found at:\n  " + "\n  ".join(offenders)
