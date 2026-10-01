"""Rendering a dataset's metadata as readable HTML.

The catalog snapshot already holds the full description, bands, licence and
citation, so the details view works offline -- v0.1 had to call ``getInfo()`` and
showed a wall of raw dict output when that failed.
"""

from __future__ import annotations

import html

from ..core import dates
from ..core.catalog import Dataset

_MAX_BANDS_SHOWN = 60


def _escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def _markdown_to_html(text: str) -> str:
    """Just enough markdown for the catalog's descriptions.

    Google's descriptions are markdown with embedded HTML entities (``NO<sub>2
    </sub>``) and inline links.  Rather than pull in a markdown dependency, this
    handles the three constructs that actually appear: links, paragraphs and
    bullet lists.  Existing HTML is passed through, which is what makes the
    subscripts render.
    """
    import re

    text = text or ""
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.startswith(("*", "-")) for line in lines):
            items = "".join(f"<li>{line.lstrip('*- ').strip()}</li>" for line in lines)
            paragraphs.append(f"<ul>{items}</ul>")
        else:
            paragraphs.append("<p>{}</p>".format(" ".join(lines)))
    return "".join(paragraphs)


def _row(label: str, value: object) -> str:
    return (
        f"<tr><td style='padding-right:12px;vertical-align:top'><b>{_escape(label)}"
        f"</b></td><td style='vertical-align:top'>{value}</td></tr>"
    )


def _band_table(dataset: Dataset) -> str:
    if not dataset.bands:
        return ""
    header = (
        "<tr><th align='left'>Band</th><th align='left'>Units</th>"
        "<th align='left'>Resolution</th><th align='left'>Range</th></tr>"
    )
    rows = []
    for band in dataset.bands[:_MAX_BANDS_SHOWN]:
        value_range = ""
        if band.minimum is not None or band.maximum is not None:
            low = band.minimum if band.minimum is not None else "?"
            high = band.maximum if band.maximum is not None else "?"
            value_range = f"{low} … {high}"
        rows.append(
            "<tr>"
            f"<td><code>{_escape(band.name)}</code></td>"
            f"<td>{_escape(band.units or '')}</td>"
            f"<td>{f'{band.gsd:g} m' if band.gsd else ''}</td>"
            f"<td>{_escape(value_range)}</td>"
            "</tr>"
        )
    more = ""
    if len(dataset.bands) > _MAX_BANDS_SHOWN:
        more = f"<p><i>… and {len(dataset.bands) - _MAX_BANDS_SHOWN} more bands.</i></p>"
    return (
        f"<h3>Bands ({len(dataset.bands)})</h3>"
        f"<table cellspacing='4'>{header}{''.join(rows)}</table>{more}"
    )


def _visualisation_list(dataset: Dataset) -> str:
    if not dataset.visualizations:
        return ""
    items = []
    for viz in dataset.visualizations:
        parts = [_escape(viz.name)]
        if viz.bands:
            parts.append("<code>" + ", ".join(_escape(b) for b in viz.bands) + "</code>")
        if viz.min is not None and viz.max is not None:
            parts.append(f"{viz.min:g} … {viz.max:g}")
        if viz.palette:
            swatches = "".join(
                "<span style='background:{};padding:0 6px'>&nbsp;</span>".format(
                    colour if colour.startswith("#") or colour.isalpha() else "#" + colour
                )
                for colour in viz.palette
            )
            parts.append(swatches)
        items.append("<li>" + " &middot; ".join(parts) + "</li>")
    return f"<h3>Suggested rendering</h3><ul>{''.join(items)}</ul>"


def dataset_html(dataset: Dataset, server_info: dict | None = None) -> str:
    """Full details page for a dataset."""
    rows = [
        _row("Earth Engine id", f"<code>{_escape(dataset.id)}</code>"),
        _row("Type", _escape(dataset.type)),
        _row("Available", _escape(dataset.availability)),
    ]
    if dataset.cadence:
        rows.append(_row("Updated", _escape(dataset.cadence)))
    if dataset.publisher:
        rows.append(_row("Publisher", _escape(dataset.publisher)))
    resolution = dataset.default_resolution()
    if resolution:
        rows.append(_row("Native resolution", f"{resolution:g} m"))
    if dataset.bbox:
        west, south, east, north = dataset.bbox
        extent = (
            "global"
            if dataset.is_global
            else f"{west:g}, {south:g} to {east:g}, {north:g}"
        )
        rows.append(_row("Extent", _escape(extent)))
    if dataset.tags:
        rows.append(_row("Tags", _escape(", ".join(dataset.tags))))
    if dataset.license:
        rows.append(_row("Licence", _escape(dataset.license)))
    if dataset.doi:
        rows.append(
            _row(
                "DOI",
                f'<a href="https://doi.org/{_escape(dataset.doi)}">{_escape(dataset.doi)}</a>',
            )
        )
    if dataset.doc_url:
        rows.append(
            _row(
                "Documentation",
                f'<a href="{_escape(dataset.doc_url)}">{_escape(dataset.doc_url)}</a>',
            )
        )

    parts = [
        f"<h2>{_escape(dataset.title)}</h2>",
        f"<table cellspacing='4'>{''.join(rows)}</table>",
    ]

    if dataset.deprecated:
        parts.append(
            "<p style='padding:6px'><b>This dataset is deprecated.</b> Google "
            "still serves it, but it is no longer updated and may be withdrawn. "
            "Prefer a current version where one exists.</p>"
        )

    if dataset.description:
        parts.append("<h3>Description</h3>")
        parts.append(_markdown_to_html(dataset.description))

    parts.append(_band_table(dataset))
    parts.append(_visualisation_list(dataset))

    if dataset.citation:
        parts.append("<h3>Citation</h3>")
        parts.append(_markdown_to_html(dataset.citation))
    if dataset.terms_of_use:
        parts.append("<h3>Terms of use</h3>")
        parts.append(_markdown_to_html(dataset.terms_of_use))

    if server_info:
        parts.append(_server_section(server_info))

    return "".join(part for part in parts if part)


def _server_section(info: dict) -> str:
    """Live figures from Earth Engine, when the user asked for them."""
    rows = []
    properties = info.get("properties") or {}
    features = info.get("features")
    if isinstance(features, list):
        rows.append(_row("Sample images returned", len(features)))
        if features:
            first = features[0].get("properties") or {}
            start = dates.parse(first.get("system:time_start"))
            if start:
                rows.append(_row("First sampled image", start.strftime("%Y-%m-%d %H:%M")))
    for key in ("system:asset_size", "system:index", "version"):
        if key in properties:
            rows.append(_row(key, _escape(properties[key])))
    if not rows:
        return ""
    return f"<h3>From Earth Engine</h3><table cellspacing='4'>{''.join(rows)}</table>"
