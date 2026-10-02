"""Checks on the repository's own documentation.

These exist because of a real mistake: Windows paths written into the docs through
a shell heredoc had their ``\\bin`` and ``\\api_audit.py`` interpreted as escape
sequences, leaving a literal backspace and bell character in the file.  The
rendered instruction read ``C:\\OSGeo4Win\\python-qgis-ltr.bat toolspi_audit.py``,
which is not a command anybody could run, and it survived a commit because nothing
was looking.
"""

from __future__ import annotations

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Documentation and configuration a user or contributor actually reads.
TEXT_SUFFIXES = (".md", ".txt", ".toml", ".yml", ".cfg", ".py")

SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache"}


def text_files() -> list[str]:
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found.extend(
            os.path.join(root, name) for name in files if name.endswith(TEXT_SUFFIXES)
        )
    return sorted(found)


FILES = text_files()


def relative(path: str) -> str:
    return os.path.relpath(path, REPO_ROOT).replace("\\", "/")


def test_there_are_files_to_check():
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=relative)
def test_no_stray_control_characters(path):
    """Tabs and newlines only: anything else is a mangled escape sequence."""
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    offenders = []
    for number, line in enumerate(content.splitlines(), start=1):
        for character in line:
            if ord(character) < 32 and character != "\t":
                offenders.append(f"line {number}: U+{ord(character):04X} in {line!r}")
    assert not offenders, f"{relative(path)}:\n  " + "\n  ".join(offenders)


#: Commands the docs tell people to run, which therefore have to be spelled right.
EXPECTED_TOOLS = ("api_audit.py", "smoke_test.py", "package.py", "refresh_catalog.py")


@pytest.mark.parametrize("tool", EXPECTED_TOOLS)
def test_documented_tools_exist(tool):
    """A README that names a script we do not ship is worse than no README."""
    assert os.path.exists(os.path.join(REPO_ROOT, "tools", tool))


def _doc(name: str) -> str:
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("name", ["README.md", "CONTRIBUTING.md"])
def test_referenced_tool_paths_are_real(name):
    """Every ``tools/x.py`` or ``tools\\x.py`` mentioned must actually exist."""
    content = _doc(name)
    mentioned = set(re.findall(r"tools[/\\]([A-Za-z0-9_]+\.py)", content))
    assert mentioned, f"{name} mentions no tools at all"
    missing = [
        tool
        for tool in sorted(mentioned)
        if not os.path.exists(os.path.join(REPO_ROOT, "tools", tool))
    ]
    assert not missing, f"{name} refers to missing tools: {missing}"


def test_windows_paths_in_docs_are_well_formed():
    """An OSGeo4W launcher path must survive into the file intact."""
    for name in ("README.md", "CONTRIBUTING.md"):
        content = _doc(name)
        for match in re.finditer(r"C:\\OSGeo4W\S*", content):
            path = match.group(0)
            assert "\\bin\\" in path, f"{name}: {path!r} lost its 'bin' segment"
            assert all(ord(c) >= 32 for c in path), f"{name}: {path!r} is mangled"


def test_readme_documents_how_to_build_and_install():
    """The packaging instructions are the thing most readers come for."""
    content = _doc("README.md")
    for fragment in (
        "tools/package.py",
        "Install from ZIP",
        "metadata.txt",
        "python/plugins",
    ):
        assert fragment in content, f"README.md no longer mentions {fragment!r}"


def test_readme_version_claims_match_the_plugin():
    """Stop the README quoting a test count or version that has moved on."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(
        os.path.join(REPO_ROOT, "ee_s5p_plugin", "metadata.txt"), encoding="utf-8"
    )
    version = parser["general"]["version"]
    # The README should not pin a version that disagrees with the plugin.
    for stale in re.findall(r"ee_s5p_plugin-(\d+\.\d+\.\d+)\.zip", _doc("README.md")):
        assert stale == version, (
            f"README.md names ee_s5p_plugin-{stale}.zip but the plugin is {version}; "
            "use <version> instead of pinning it"
        )
