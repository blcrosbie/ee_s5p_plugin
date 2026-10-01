"""Progress tracking and the wording shown to the user."""

from __future__ import annotations

import pytest

from core import progress as prog


def snapshot(**overrides) -> prog.ProgressSnapshot:
    base = {
        "phase": "Extracting",
        "done": 10,
        "total": 100,
        "unit": "tile",
        "elapsed": 20.0,
        "rate": 0.5,
    }
    base.update(overrides)
    return prog.ProgressSnapshot(**base)


class TestPercent:
    def test_basic(self):
        assert snapshot(done=25, total=100).percent == 25
        assert snapshot(done=1, total=3).percent == 33

    def test_clamped(self):
        assert snapshot(done=200, total=100).percent == 100
        assert snapshot(done=-5, total=100).percent == 0

    def test_none_when_unknowable(self):
        assert snapshot(indeterminate=True).percent is None
        assert snapshot(total=0).percent is None


class TestRemainingTime:
    def test_uses_the_measured_rate_once_there_are_samples(self):
        # 10 done in 20 s is 0.5/s, so 90 remaining is 180 s.
        assert snapshot(done=10, total=100, rate=0.5).remaining_seconds == 180.0

    def test_falls_back_to_the_planned_rate_while_samples_are_thin(self):
        """Two data points would give a wild ETA, so the plan's figure is used."""
        early = snapshot(done=1, total=100, rate=10.0, expected_rate=1.0)
        assert early.effective_rate == 1.0
        assert early.remaining_seconds == 99.0

    def test_measured_rate_wins_once_trusted(self):
        later = snapshot(done=50, total=100, rate=2.0, expected_rate=0.1)
        assert later.effective_rate == 2.0

    def test_none_when_finished(self):
        assert snapshot(done=100, total=100).remaining_seconds is None

    def test_none_when_indeterminate(self):
        assert snapshot(indeterminate=True).remaining_seconds is None

    def test_none_without_any_rate(self):
        assert snapshot(done=0, rate=0.0, expected_rate=0.0).remaining_seconds is None


class TestStatusLine:
    def test_reports_position_records_rate_and_eta(self):
        text = snapshot(done=143, total=545, records=28_600, rate=2.1).status_line()
        assert "143 of 545" in text
        assert "28,600 records" in text
        assert "tiles/s" in text
        assert "left" in text

    def test_thousands_are_grouped(self):
        assert "1,234 of 5,678" in snapshot(done=1234, total=5678).status_line()

    def test_throttling_is_explained_not_just_slow(self):
        """A stalled bar with no explanation looks like a hang."""
        text = snapshot(throttled=True).status_line()
        assert "rate limited" in text
        assert "backing off" in text

    def test_throttling_replaces_the_rate_but_keeps_the_eta(self):
        text = snapshot(throttled=True, done=50, total=100, rate=1.0).status_line()
        assert "tiles/s" not in text
        assert "left" in text

    def test_indeterminate_shows_phase_and_elapsed(self):
        text = snapshot(
            indeterminate=True, phase="Extracting NO2", elapsed=42.0
        ).status_line()
        assert "Extracting NO2" in text
        assert "elapsed" in text
        assert "of" not in text

    def test_finished_reports_total_time(self):
        text = snapshot(done=100, total=100, elapsed=95.0).status_line()
        assert "done in" in text
        assert "1 min 35 s" in text

    def test_failures_are_surfaced(self):
        assert "3 failed" in snapshot(failures=3).status_line()

    def test_no_failure_text_when_clean(self):
        assert "failed" not in snapshot(failures=0).status_line()

    def test_a_note_is_appended(self):
        assert "split into 7" in snapshot(note="split into 7 smaller tiles").status_line()

    def test_fast_rates_drop_the_decimal(self):
        text = snapshot(done=820, total=1162, rate=180.0, unit="dataset").status_line()
        assert "180 datasets/s" in text

    def test_slow_jobs_report_per_minute_not_per_second(self):
        text = snapshot(done=10, total=100, rate=0.05).status_line()
        assert "/min" in text
        assert "/s" not in text

    def test_records_omitted_when_none_yet(self):
        assert "records" not in snapshot(records=0).status_line()


class TestBarFormat:
    def test_determinate_includes_the_percent_placeholder(self):
        assert "%p%" in snapshot().bar_format()

    def test_indeterminate_has_no_placeholder(self):
        assert "%p" not in snapshot(indeterminate=True).bar_format()


class TestRoundTrip:
    def test_survives_the_dict_hop_between_threads(self):
        original = snapshot(done=7, records=99, note="x", throttled=True)
        assert prog.ProgressSnapshot.from_dict(original.to_dict()) == original

    def test_unknown_keys_are_ignored(self):
        payload = snapshot().to_dict()
        payload["something_new"] = 1
        assert prog.ProgressSnapshot.from_dict(payload).done == 10

    def test_partial_payload_uses_defaults(self):
        assert prog.ProgressSnapshot.from_dict({"done": 5}).done == 5


class TestTracker:
    def test_accumulates(self):
        tracker = prog.ProgressTracker(total=10)
        tracker.advance(records=100)
        tracker.advance(records=50, failures=1)
        assert tracker.done == 2
        assert tracker.records == 150
        assert tracker.failures == 1

    def test_total_can_grow_when_tiles_are_split(self):
        tracker = prog.ProgressTracker(total=10)
        tracker.add_total(7)
        assert tracker.total == 17

    def test_negative_growth_is_ignored(self):
        tracker = prog.ProgressTracker(total=10)
        tracker.add_total(-5)
        assert tracker.total == 10

    def test_rate_is_measured(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=100)
        clock["t"] = 1010.0
        tracker.advance(units=5)
        assert tracker.elapsed == 10.0
        assert tracker.rate == 0.5

    def test_rate_is_zero_before_anything_finishes(self):
        assert prog.ProgressTracker(total=10).rate == 0.0

    def test_throttled_expires(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=10)
        tracker.mark_throttled(5.0)
        assert tracker.throttled
        clock["t"] = 6.0
        assert not tracker.throttled

    def test_zero_backoff_does_not_mark_throttled(self):
        tracker = prog.ProgressTracker(total=10)
        tracker.mark_throttled(0)
        assert not tracker.throttled

    def test_snapshot_reflects_state(self):
        tracker = prog.ProgressTracker(total=20, phase="Extracting", unit="tile")
        tracker.advance(records=5)
        tracker.set_note("hello")
        taken = tracker.snapshot()
        assert taken.done == 1
        assert taken.total == 20
        assert taken.records == 5
        assert taken.note == "hello"
        assert taken.phase == "Extracting"

    def test_snapshot_is_immutable(self):
        taken = prog.ProgressTracker(total=5).snapshot()
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            taken.done = 3


class TestRedrawThrottling:
    """A 500 tile job must not repaint the dock hundreds of times a second."""

    def test_first_update_always_emits(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=100)
        tracker.advance()
        assert tracker.should_emit()

    def test_rapid_updates_are_collapsed(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=100)
        tracker.advance()
        tracker.should_emit()
        emitted = 0
        for _ in range(50):
            clock["t"] += 0.001
            tracker.advance()
            if tracker.should_emit():
                emitted += 1
        assert emitted == 0, "updates should have been collapsed"

    def test_updates_resume_after_the_interval(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=100)
        tracker.advance()
        tracker.should_emit()
        clock["t"] += prog.MIN_REDRAW_INTERVAL + 0.01
        tracker.advance()
        assert tracker.should_emit()

    def test_the_final_update_always_emits(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
        tracker = prog.ProgressTracker(total=3)
        tracker.advance()
        tracker.should_emit()
        clock["t"] += 0.001
        tracker.advance()
        tracker.should_emit()
        clock["t"] += 0.001
        tracker.advance()  # now done == total
        assert tracker.should_emit(), "the completed state must reach the panel"


class TestRateFromEstimate:
    def test_converts_a_duration_to_a_rate(self):
        assert prog.rate_from_estimate(100, 50.0) == 2.0

    @pytest.mark.parametrize("units, seconds", [(0, 10), (10, 0), (-1, 5), (5, -1)])
    def test_degenerate_inputs_give_zero(self, units, seconds):
        assert prog.rate_from_estimate(units, seconds) == 0.0


def test_a_realistic_job_reads_sensibly(monkeypatch):
    """End to end: the line a user would actually see partway through."""
    clock = {"t": 0.0}
    monkeypatch.setattr(prog.time, "perf_counter", lambda: clock["t"])
    tracker = prog.ProgressTracker(
        total=545,
        phase="Extracting",
        unit="tile",
        expected_rate=prog.rate_from_estimate(545, 1090.0),
    )
    for _ in range(143):
        clock["t"] += 0.5
        tracker.advance(records=200)
    text = tracker.snapshot().status_line()
    assert "Tile 143 of 545" in text
    assert "28,600 records" in text
    assert "left" in text
    assert tracker.snapshot().percent == 26
