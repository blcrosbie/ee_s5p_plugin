"""Date handling: the formats Earth Engine mixes, and open-ended extents."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import dates


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2018-06-28T10:24:07Z", datetime(2018, 6, 28, 10, 24, 7, tzinfo=timezone.utc)),
        ("2018-07-10T11:17:44", datetime(2018, 7, 10, 11, 17, 44, tzinfo=timezone.utc)),
        ("2020-01-02", datetime(2020, 1, 2, tzinfo=timezone.utc)),
        ("20180710T111744", datetime(2018, 7, 10, 11, 17, 44, tzinfo=timezone.utc)),
        (
            "2018-06-28T10:24:07.123Z",
            datetime(2018, 6, 28, 10, 24, 7, 123000, tzinfo=timezone.utc),
        ),
        ("2020-01-02 03:04:05", datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
    ],
)
def test_parse_known_formats(text, expected):
    assert dates.parse(text) == expected


@pytest.mark.parametrize(
    "value", [None, "", "   ", "Present", "present", "nonsense", True, object()]
)
def test_parse_returns_none_for_unbounded_or_unusable(value):
    assert dates.parse(value) is None


def test_parse_is_always_timezone_aware():
    """Naive values gain UTC, so comparisons never raise TypeError."""
    naive = dates.parse("2018-07-10T11:17:44")
    aware = dates.parse("2018-06-28T10:24:07Z")
    assert naive.tzinfo is not None and aware.tzinfo is not None
    assert aware < naive  # the comparison the original code crashed on


def test_parse_accepts_epoch_milliseconds():
    """``system:time_start`` arrives in milliseconds."""
    assert dates.parse(1530181447000) == datetime(
        2018, 6, 28, 10, 24, 7, tzinfo=timezone.utc
    )


def test_to_ee_date_and_add_days():
    assert dates.to_ee_date("2018-06-28T10:24:07Z") == "2018-06-28"
    assert dates.add_days("2020-02-28", 2) == "2020-03-01"  # leap year
    assert dates.add_days("2020-01-01", -1) == "2019-12-31"
    assert dates.add_days(None, 1) is None


def test_day_span():
    assert dates.day_span("2020-01-01", "2020-01-31") == 30
    assert dates.day_span("2020-01-01", None) is None


class TestOverlaps:
    """A missing bound must mean "open", never "excluded"."""

    def test_windows_that_intersect(self):
        assert dates.overlaps("2018-01-01", "2020-01-01", "2019-01-01", "2019-06-01")

    def test_filter_entirely_after_dataset(self):
        assert not dates.overlaps("2018-01-01", "2018-12-31", "2019-01-01", "2019-06-01")

    def test_filter_entirely_before_dataset(self):
        assert not dates.overlaps("2020-01-01", "2021-01-01", "2018-01-01", "2018-06-01")

    def test_touching_boundaries_count_as_overlap(self):
        assert dates.overlaps("2020-01-01", "2020-06-01", "2020-06-01", "2020-12-01")

    def test_open_ended_dataset_matches_recent_filter(self):
        assert dates.overlaps("2018-01-01", None, "2026-01-01", "2026-06-01")

    def test_unknown_dataset_extent_is_never_filtered_out(self):
        assert dates.overlaps(None, None, "2019-01-01", "2019-06-01")


def test_image_index_time():
    assert dates.image_index_time("20180710T111744") == "2018-07-10 11:17:44"
    assert (
        dates.image_index_time("20180710T111744_20180710T125914") == "2018-07-10 11:17:44"
    )
    assert dates.image_index_time("20180710T111744", with_time=False) == "2018-07-10"
    assert dates.image_index_time("") is None
    assert dates.image_index_time("not-a-date") is None


def test_describe_range_marks_ongoing_collections():
    assert dates.describe_range("2018-06-28T10:24:07Z", None).endswith("present")
    assert "2020-01-01" in dates.describe_range("2018-06-28", "2020-01-01")
    assert dates.describe_range(None, None) == "unknown"
