"""Worker policy, failure classification, backoff, and the job runner."""

from __future__ import annotations

import random
import threading
import time

import pytest

from core import concurrency as conc


class TestWorkerPolicy:
    def test_cap_is_half_the_cpus(self, monkeypatch):
        monkeypatch.setattr(conc, "cpu_count", lambda: 8)
        assert conc.max_workers() == 4

    def test_cap_is_at_least_one_on_a_single_core(self, monkeypatch):
        monkeypatch.setattr(conc, "cpu_count", lambda: 1)
        assert conc.max_workers() == 1
        assert conc.worker_choices() == [1]

    def test_cap_is_bounded_regardless_of_cpu_count(self, monkeypatch):
        """Past a point more workers only earn more 429s."""
        monkeypatch.setattr(conc, "cpu_count", lambda: 128)
        assert conc.max_workers() == conc.ABSOLUTE_MAX_WORKERS

    def test_share_is_configurable(self, monkeypatch):
        monkeypatch.setattr(conc, "cpu_count", lambda: 8)
        assert conc.max_workers(share=0.25) == 2

    def test_default_is_conservative(self, monkeypatch):
        monkeypatch.setattr(conc, "cpu_count", lambda: 16)
        assert conc.default_workers() == 2
        assert conc.default_workers() <= conc.max_workers()

    def test_choices_start_at_sequential(self, monkeypatch):
        monkeypatch.setattr(conc, "cpu_count", lambda: 8)
        assert conc.worker_choices() == [1, 2, 3, 4]

    def test_describe_workers(self):
        assert "sequential" in conc.describe_workers(1)
        assert "4 parallel" in conc.describe_workers(4)


class TestClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "HTTP 429 Too Many Requests",
            "Rate limit exceeded",
            "Quota exceeded for quota metric",
            "Too many concurrent aggregations",
            "RESOURCE_EXHAUSTED",
        ],
    )
    def test_rate_limits_are_recognised(self, message):
        assert conc.is_rate_limited(message)
        assert conc.should_retry(message)
        assert not conc.should_subdivide(message)

    @pytest.mark.parametrize(
        "message",
        ["503 Service Unavailable", "Backend Error", "Connection reset by peer"],
    )
    def test_transient_failures_are_retried(self, message):
        assert conc.is_transient(message)
        assert conc.should_retry(message)

    @pytest.mark.parametrize(
        "message",
        [
            "Too many values: 658739 points x 1 bands x 4 images > 1048576.",
            "User memory limit exceeded.",
            "Collection query aborted after accumulating over 5000 elements.",
        ],
    )
    def test_oversized_requests_are_subdivided_not_retried(self, message):
        """Retrying an over-large request just burns attempts."""
        assert conc.is_too_large(message)
        assert conc.should_subdivide(message)
        assert not conc.should_retry(message)

    def test_timeout_counts_as_oversized_first(self):
        """'timed out' is ambiguous; splitting is the useful response."""
        assert conc.should_retry("Computation timed out.")
        assert conc.should_subdivide("Too many values, computation timed out")

    def test_unknown_errors_are_not_retried(self):
        assert not conc.should_retry("Something else entirely")
        assert not conc.should_subdivide("Something else entirely")

    def test_accepts_exceptions_as_well_as_strings(self):
        assert conc.is_rate_limited(RuntimeError("HTTP 429"))
        assert conc.is_too_large(ValueError("Too many values: 1 > 0"))


class TestRetryPolicy:
    def test_delay_doubles(self):
        policy = conc.RetryPolicy(base_delay=1.0, jitter=0.0)
        assert policy.delay_for(1) == 1.0
        assert policy.delay_for(2) == 2.0
        assert policy.delay_for(3) == 4.0

    def test_delay_is_capped(self):
        policy = conc.RetryPolicy(base_delay=1.0, max_delay=5.0, jitter=0.0)
        assert policy.delay_for(10) == 5.0

    def test_attempt_zero_is_immediate(self):
        assert conc.RetryPolicy().delay_for(0) == 0.0

    def test_jitter_spreads_retries(self):
        """Without jitter, parallel workers retry in lockstep."""
        policy = conc.RetryPolicy(base_delay=10.0, jitter=0.5)
        rng = random.Random(1)
        delays = {policy.delay_for(1, rng) for _ in range(20)}
        assert len(delays) > 10
        assert all(5.0 <= d <= 15.0 for d in delays)

    def test_jitter_never_goes_negative(self):
        policy = conc.RetryPolicy(base_delay=1.0, jitter=2.0)
        rng = random.Random(0)
        assert all(policy.delay_for(1, rng) >= 0 for _ in range(50))


class TestAdaptiveGate:
    def test_limits_requests_in_flight(self):
        gate = conc.AdaptiveGate(limit=2)
        assert gate.acquire() and gate.acquire()
        assert not gate.acquire(timeout=0.05)
        gate.release()
        assert gate.acquire(timeout=0.5)

    def test_penalise_halves_the_allowance(self):
        gate = conc.AdaptiveGate(limit=8)
        assert gate.penalise() == 4
        assert gate.penalise() == 2
        assert gate.penalise() == 1

    def test_penalise_never_goes_below_the_floor(self):
        gate = conc.AdaptiveGate(limit=2, floor=1)
        for _ in range(10):
            gate.penalise()
        assert gate.limit == 1

    def test_penalise_pauses_every_worker(self):
        """Rate limiting applies to the account, so all workers must ease off."""
        gate = conc.AdaptiveGate(limit=4)
        gate.penalise(pause_seconds=0.2)
        assert gate.paused_for > 0
        slept = []
        gate.wait_if_paused(sleep=slept.append)
        assert slept and slept[0] > 0

    def test_no_pause_when_calm(self):
        gate = conc.AdaptiveGate(limit=4)
        slept = []
        assert gate.wait_if_paused(sleep=slept.append) == 0
        assert not slept

    def test_reward_recovers_towards_the_ceiling(self):
        gate = conc.AdaptiveGate(limit=4, ceiling=4, recover_after=2)
        gate.penalise()
        assert gate.limit == 2
        for _ in range(4):
            gate.reward()
        assert gate.limit == 4

    def test_reward_never_exceeds_the_ceiling(self):
        gate = conc.AdaptiveGate(limit=2, ceiling=2, recover_after=1)
        for _ in range(10):
            gate.reward()
        assert gate.limit == 2

    def test_counts_penalties(self):
        gate = conc.AdaptiveGate(limit=4)
        gate.penalise()
        gate.penalise()
        assert gate.penalties == 2

    def test_is_thread_safe(self):
        gate = conc.AdaptiveGate(limit=4)
        peak = {"value": 0}
        live = {"value": 0}
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                gate.acquire()
                with lock:
                    live["value"] += 1
                    peak["value"] = max(peak["value"], live["value"])
                time.sleep(0.001)
                with lock:
                    live["value"] -= 1
                gate.release()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert peak["value"] <= 4, f"allowance exceeded: {peak['value']}"


class TestRunJobs:
    def test_sequential_runs_everything_in_order(self):
        seen = []
        report = conc.run_jobs([1, 2, 3], seen.append, workers=1)
        assert seen == [1, 2, 3]
        assert len(report.succeeded) == 3
        assert report.workers == 1

    def test_sequential_uses_no_thread_pool(self):
        """workers=1 must be genuinely single-threaded, not a pool of one."""
        main = threading.get_ident()
        threads = set()
        conc.run_jobs([1, 2, 3], lambda _j: threads.add(threading.get_ident()), workers=1)
        assert threads == {main}

    def test_parallel_runs_everything(self):
        report = conc.run_jobs(list(range(20)), lambda j: j * 2, workers=4)
        assert len(report.succeeded) == 20
        assert sorted(o.result for o in report.succeeded) == [j * 2 for j in range(20)]

    def test_parallel_actually_overlaps(self):
        barrier = threading.Barrier(4, timeout=5)

        def work(job):
            barrier.wait()  # only completes if four run at once
            return job

        report = conc.run_jobs(list(range(8)), work, workers=4)
        assert len(report.succeeded) == 8

    def test_empty_job_list(self):
        report = conc.run_jobs([], lambda j: j, workers=4)
        assert report.outcomes == []

    def test_results_record_timing(self):
        """Durations use perf_counter: monotonic's ~16 ms granularity on Windows
        reads a fast request as zero, which would poison the running average."""

        def slow(job):
            time.sleep(0.02)
            return job

        report = conc.run_jobs([1], slow, workers=1)
        assert report.succeeded[0].seconds > 0
        assert report.succeeded[0].seconds == pytest.approx(0.02, abs=0.05)
        assert report.elapsed > 0

    def test_progress_reports_done_and_total(self):
        seen = []
        conc.run_jobs(
            [1, 2, 3],
            lambda j: j,
            workers=1,
            progress=lambda outcome, done, total: seen.append((done, total)),
        )
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_rate_limited_jobs_are_retried_then_succeed(self):
        attempts = {"n": 0}

        def flaky(job):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return "ok"

        report = conc.run_jobs(
            [1],
            flaky,
            workers=1,
            retry=conc.RetryPolicy(attempts=4, base_delay=0, jitter=0),
            sleep=lambda _s: None,
        )
        assert report.succeeded[0].result == "ok"
        assert report.succeeded[0].attempts == 3
        assert report.rate_limit_events == 2

    def test_retries_give_up_after_the_limit(self):
        def always_throttled(job):
            raise RuntimeError("429 rate limit")

        report = conc.run_jobs(
            [1],
            always_throttled,
            workers=1,
            retry=conc.RetryPolicy(attempts=3, base_delay=0, jitter=0),
            sleep=lambda _s: None,
        )
        assert len(report.failed) == 1
        assert report.failed[0].attempts == 3

    def test_oversized_jobs_are_flagged_not_retried(self):
        calls = {"n": 0}

        def too_big(job):
            calls["n"] += 1
            raise RuntimeError("Too many values: 2000000 points x 1 bands > 1048576.")

        report = conc.run_jobs(
            [1],
            too_big,
            workers=1,
            retry=conc.RetryPolicy(attempts=4, base_delay=0, jitter=0),
            sleep=lambda _s: None,
        )
        assert calls["n"] == 1, "an oversized request must not be retried"
        assert len(report.oversized) == 1
        assert report.oversized[0].needs_subdivision

    def test_unretryable_errors_fail_immediately(self):
        calls = {"n": 0}

        def broken(job):
            calls["n"] += 1
            raise ValueError("no such band")

        report = conc.run_jobs(
            [1],
            broken,
            workers=1,
            retry=conc.RetryPolicy(attempts=4),
            sleep=lambda _s: None,
        )
        assert calls["n"] == 1
        assert len(report.failed) == 1

    def test_cancellation_stops_a_sequential_run_early(self):
        done = []

        def work(job):
            done.append(job)
            return job

        report = conc.run_jobs(
            list(range(100)), work, workers=1, is_cancelled=lambda: len(done) >= 3
        )
        assert len(done) <= 4
        assert report.cancelled

    def test_one_failure_does_not_stop_the_rest(self):
        def sometimes(job):
            if job == 5:
                raise ValueError("bad tile")
            return job

        report = conc.run_jobs(list(range(10)), sometimes, workers=4)
        assert len(report.succeeded) == 9
        assert len(report.failed) == 1

    def test_report_summary_is_readable(self):
        report = conc.run_jobs([1, 2], lambda j: j, workers=2)
        text = report.summary()
        assert "2 of 2 succeeded" in text
        assert "elapsed" in text


class TestTimingEstimates:
    def test_sequential_is_jobs_times_latency(self):
        assert conc.estimate_seconds(10, workers=1, seconds_per_request=2.0) == 20.0

    def test_parallel_is_faster_but_sublinear(self):
        one = conc.estimate_seconds(100, 1, 2.0)
        four = conc.estimate_seconds(100, 4, 2.0)
        assert four < one
        assert one / four < 4, "speed-up should not be modelled as perfect"
        assert one / four > 2

    def test_the_user_asked_for_two_to_three_times(self):
        """Four workers should land in the 2-3x+ band the feature promises."""
        assert 2.0 < conc.speedup(500, 4) < 4.0

    def test_no_jobs_takes_no_time(self):
        assert conc.estimate_seconds(0, 4) == 0.0

    def test_speedup_of_one_worker_is_one(self):
        assert conc.speedup(50, 1) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0.4, "under a second"),
            (5, "5 s"),
            (95, "1 min 35 s"),
            (120, "2 min"),
            (3600, "1 h"),
            (3900, "1 h 5 min"),
        ],
    )
    def test_format_duration(self, seconds, expected):
        assert conc.format_duration(seconds) == expected

    def test_measure_averages_successful_timings(self):
        outcomes = [
            conc.Outcome(key=1, seconds=1.0),
            conc.Outcome(key=2, seconds=3.0),
            conc.Outcome(key=3, error=RuntimeError("x"), seconds=99.0),
        ]
        assert conc.measure(outcomes) == 2.0

    def test_measure_falls_back_with_no_data(self):
        assert conc.measure([]) == conc.DEFAULT_SECONDS_PER_REQUEST
