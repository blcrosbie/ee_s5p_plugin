"""Request validation, local size estimation and error interpretation."""

from __future__ import annotations

import pytest

from core import request as req

SMALL_BBOX = (-80.25, 40.0, -80.0, 40.1)  # ~21 x 11 km near Pittsburgh
WORLD = (-180.0, -90.0, 180.0, 90.0)


def make(**overrides):
    kwargs = {
        "dataset_id": "COPERNICUS/S5P/OFFL/L3_NO2",
        "dataset_type": "ImageCollection",
        "bands": ("NO2_column_number_density",),
        "start": "2020-01-01",
        "end": "2020-01-02",
        "resolution_m": 1113.2,
        "bbox": SMALL_BBOX,
    }
    kwargs.update(overrides)
    return req.ExtractRequest(**kwargs)


class TestValidation:
    def test_a_complete_request_is_accepted(self):
        assert make().dataset_id

    def test_missing_dataset_id(self):
        with pytest.raises(req.RequestError, match="No dataset"):
            make(dataset_id="")

    def test_unknown_dataset_type(self):
        with pytest.raises(req.RequestError, match="Unsupported dataset type"):
            make(dataset_type="BigQueryTable")

    def test_raster_without_a_band(self):
        with pytest.raises(req.RequestError, match="at least one band"):
            make(bands=())

    def test_raster_without_a_resolution(self):
        with pytest.raises(req.RequestError, match="resolution"):
            make(resolution_m=0)

    def test_collection_without_dates(self):
        with pytest.raises(req.RequestError, match="start and end"):
            make(start=None)

    def test_end_before_start(self):
        with pytest.raises(req.RequestError, match="end date is before"):
            make(start="2020-06-01", end="2020-01-01")

    def test_feature_collection_needs_no_band_or_resolution(self):
        request = req.ExtractRequest(
            dataset_id="EU/REGIONS",
            dataset_type="FeatureCollection",
            bbox=SMALL_BBOX,
        )
        assert request.estimate().values == 0

    def test_single_image_needs_no_dates(self):
        request = req.ExtractRequest(
            dataset_id="X/IMG",
            dataset_type="Image",
            bands=("b1",),
            resolution_m=30,
            bbox=SMALL_BBOX,
        )
        assert request.estimate().images == 1


class TestEstimate:
    def test_small_area_at_native_resolution_fits(self):
        estimate = make().estimate()
        assert estimate.fits
        assert estimate.values > 0

    def test_whole_world_at_fine_resolution_does_not_fit(self):
        estimate = make(bbox=WORLD, resolution_m=1113.2).estimate()
        assert not estimate.fits
        assert estimate.over_by > 1

    def test_values_scale_with_bands(self):
        one = make().estimate().values
        two = make(bands=("a", "b")).estimate().values
        assert two == 2 * one

    def test_points_scale_with_the_inverse_square_of_resolution(self):
        coarse = make(resolution_m=2000).estimate().points
        fine = make(resolution_m=1000).estimate().points
        assert fine == pytest.approx(4 * coarse, rel=0.02)

    def test_unknown_area_gives_an_unknown_estimate(self):
        assert make(bbox=None).estimate().points == 0

    def test_describe_is_readable(self):
        text = make().estimate().describe()
        assert "points" in text and "limit" in text

    def test_warning_threshold(self):
        assert req.Estimate(points=1_000_000, bands=1, images=1).should_warn
        assert not req.Estimate(points=10, bands=1, images=1).should_warn


class TestSuggestions:
    def test_suggested_resolution_brings_the_request_under_the_limit(self):
        request = make(bbox=WORLD, resolution_m=1113.2)
        estimate = request.estimate()
        suggested = estimate.suggested_resolution(1113.2)
        assert suggested > 1113.2
        fixed = make(bbox=WORLD, resolution_m=suggested).estimate()
        assert fixed.fits, f"{suggested} m still yields {fixed.values} values"

    def test_no_suggestion_when_the_request_already_fits(self):
        assert make().estimate().suggested_resolution(1113.2) is None

    def test_suggested_resolution_is_a_tidy_number(self):
        suggested = make(bbox=WORLD).estimate().suggested_resolution(1113.2)
        assert suggested == pytest.approx(round(suggested, 6))

    def test_suggested_days_shrinks_the_window(self):
        request = make(bbox=WORLD, start="2020-01-01", end="2020-12-31")
        suggested = request.estimate().suggested_days(365)
        assert suggested is not None and 1 <= suggested < 365

    def test_no_day_suggestion_when_it_fits(self):
        assert make().estimate().suggested_days(1) is None


class TestImageCount:
    def test_daily_cadence(self):
        count = req.estimate_images_in_window(
            "2020-01-01", "2020-01-11", {"type": "cadence", "unit": "day", "interval": 1}
        )
        assert count == 10

    def test_eight_day_cadence(self):
        count = req.estimate_images_in_window(
            "2020-01-01", "2020-03-01", {"type": "cadence", "unit": "day", "interval": 8}
        )
        assert count == 8

    def test_sub_daily_cadence(self):
        count = req.estimate_images_in_window(
            "2020-01-01",
            "2020-01-02",
            {"type": "cadence", "unit": "minute", "interval": 10},
        )
        assert count == 144

    def test_monthly_cadence(self):
        count = req.estimate_images_in_window(
            "2020-01-01",
            "2021-01-01",
            {"type": "cadence", "unit": "month", "interval": 1},
        )
        assert 12 <= count <= 13

    def test_no_cadence_assumes_daily(self):
        assert req.estimate_images_in_window("2020-01-01", "2020-01-11", None) == 10

    def test_unparseable_window_is_one_image(self):
        assert req.estimate_images_in_window(None, None, None) == 1

    def test_zero_length_window_is_at_least_one_image(self):
        assert req.estimate_images_in_window("2020-01-01", "2020-01-01", None) == 1


class TestExplainError:
    @pytest.mark.parametrize(
        "message, expected",
        [
            ("Too many values: 2 points x 1 bands x 4 images > 1048576.", "more data"),
            ("User memory limit exceeded.", "memory"),
            ("Computation timed out.", "gave up"),
            ("ImageCollection.load: not found.", "does not recognise"),
            ("Image.select: no such band 'x'", "not present"),
            ("Permission denied on asset", "cannot read"),
            ("Quota exceeded", "quota"),
        ],
    )
    def test_known_failures_get_plain_language(self, message, expected):
        what, advice = req.explain_error(message)
        assert expected in what.lower()
        assert advice

    def test_unknown_failure_is_passed_through(self):
        what, advice = req.explain_error("something odd")
        assert what == "something odd"
        assert advice


class TestParseLimitError:
    def test_recovers_the_real_numbers(self):
        estimate = req.parse_limit_error(
            "Too many values: 658739 points x 1 bands x 4 images > 1048576."
        )
        assert estimate.points == 658739
        assert estimate.bands == 1
        assert estimate.images == 4
        assert estimate.limit == 1048576
        assert not estimate.fits

    def test_recovered_numbers_drive_a_usable_suggestion(self):
        estimate = req.parse_limit_error(
            "Too many values: 658739 points x 3 bands x 4 images > 1048576."
        )
        assert estimate.suggested_resolution(1000) > 1000

    def test_other_errors_return_none(self):
        assert req.parse_limit_error("User memory limit exceeded") is None

    def test_malformed_limit_message_returns_none(self):
        assert req.parse_limit_error("Too many values: lots > heaps.") is None


def test_summary_mentions_the_key_parameters():
    text = make(crs="EPSG:4326").summary()
    for fragment in [
        "COPERNICUS/S5P",
        "NO2_column_number_density",
        "2020-01-01",
        "EPSG:4326",
        "km",
    ]:
        assert fragment in text
