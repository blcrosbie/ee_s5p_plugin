"""Date parsing shared by the catalog, the filters and the request builder.

Earth Engine mixes several representations in the metadata it publishes, and the
plugin has to compare them against each other:

* STAC temporal extents -- ``2018-06-28T10:24:07Z`` (RFC 3339, always UTC)
* older catalog entries  -- ``2018-07-10T11:17:44`` (naive ISO)
* image ids              -- ``20180710T111744``
* an open ended extent   -- ``None``, meaning "still being added to"

Everything here returns timezone-aware UTC :class:`~datetime.datetime` objects
so comparisons never raise the "can't compare offset-naive and offset-aware"
``TypeError`` the original code tripped over.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: What Earth Engine's ``filterDate`` accepts, and what we show in the GUI.
EE_DATE_FORMAT = "%Y-%m-%d"

_PARSE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y%m%dT%H%M%S",
    "%Y%m%d",
)

#: The value older catalog snapshots used for "no end date".
PRESENT = "Present"


def now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse(value: object) -> datetime | None:
    """Parse any of the representations above into an aware UTC datetime.

    Returns ``None`` for ``None``, for the legacy ``"Present"`` sentinel and for
    anything unparseable -- callers treat all three as "unbounded".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Earth Engine reports ``system:time_start`` in milliseconds.
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.lower() == PRESENT.lower():
        return None

    try:
        # ``fromisoformat`` handles offsets and, from 3.11, a trailing "Z".
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _PARSE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            break
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_ee_date(value: object) -> str | None:
    """Format a value as the ``YYYY-MM-DD`` string ``filterDate`` expects."""
    parsed = parse(value)
    return parsed.strftime(EE_DATE_FORMAT) if parsed else None


def add_days(value: object, days: int) -> str | None:
    """``add_days("2020-01-01", 1) == "2020-01-02"``, or ``None`` if unparseable."""
    parsed = parse(value)
    if parsed is None:
        return None
    return (parsed + timedelta(days=days)).strftime(EE_DATE_FORMAT)


def day_span(start: object, end: object) -> int | None:
    """Whole days between two values, or ``None`` if either is unusable."""
    first, last = parse(start), parse(end)
    if first is None or last is None:
        return None
    return (last - first).days


def overlaps(
    dataset_start: object,
    dataset_end: object,
    filter_start: object,
    filter_end: object,
) -> bool:
    """Do a dataset's availability and a user's filter window intersect?

    An unparseable or missing bound is treated as open, so datasets whose extent
    Google has not published stay visible instead of silently disappearing.
    """
    d_start, d_end = parse(dataset_start), parse(dataset_end)
    f_start, f_end = parse(filter_start), parse(filter_end)

    starts_after_filter = f_end is not None and d_start is not None and d_start > f_end
    ends_before_filter = f_start is not None and d_end is not None and d_end < f_start
    return not (starts_after_filter or ends_before_filter)


def image_index_time(index: str, with_time: bool = True) -> str | None:
    """Turn the timestamp embedded in an image id into something readable.

    Earth Engine image ids are commonly ``20180710T111744`` or
    ``20180710T111744_20180710T125914``; only the leading stamp is used here.
    """
    if not index:
        return None
    parsed = parse(index.split("_")[0])
    if parsed is None:
        return None
    fmt = "%Y-%m-%d %H:%M:%S" if with_time else EE_DATE_FORMAT
    return parsed.strftime(fmt)


def describe_range(start: object, end: object) -> str:
    """Human readable availability, e.g. ``2018-06-28 - present``."""
    first = to_ee_date(start)
    last = to_ee_date(end)
    if first and last:
        return f"{first} – {last}"
    if first:
        return f"{first} – present"
    if last:
        return f"? – {last}"
    return "unknown"
