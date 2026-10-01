"""Running many Earth Engine requests at once, without tripping its rate limit.

**Threads, not processes.** An Earth Engine request spends essentially all of its
time waiting on an HTTPS round trip, and Python releases the GIL while waiting, so
threads give the full speed-up with none of the cost of processes: no re-importing
QGIS per worker, no pickling ``ee`` objects (which are server-side descriptions,
not data), and no spawn-vs-fork difference between Windows and Linux.

The worker count is still capped against the CPU count, because that is the
intuitive dial and it keeps the plugin from monopolising a laptop. Be aware the
real ceiling is usually Earth Engine's own rate limit rather than the CPU: a 16
core machine will still get throttled at eight concurrent requests. That is what
:class:`AdaptiveGate` is for -- it narrows concurrency when the server pushes back
and widens it again when things are calm, so a job slows down instead of failing.

Pure Python: no Qt, no QGIS, no ``ee``.  ``sleep`` is injectable so the retry
logic can be tested without actually waiting.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

#: Never use more than this share of the machine's CPUs by default.
DEFAULT_CPU_SHARE = 0.5

#: Hard ceiling regardless of CPU count: beyond this Earth Engine throttles us
#: anyway, so more workers buy nothing but 429s.
ABSOLUTE_MAX_WORKERS = 8


def cpu_count() -> int:
    return os.cpu_count() or 1


def max_workers(share: float = DEFAULT_CPU_SHARE) -> int:
    """The most workers the plugin will offer: half the CPUs, capped."""
    allowed = int(cpu_count() * share)
    return max(1, min(allowed, ABSOLUTE_MAX_WORKERS))


def default_workers() -> int:
    """What to pre-select.

    Two is a real speed-up over sequential and is gentle enough that a first run
    is unlikely to meet the rate limit at all.  Going straight to the cap would
    make a user's first experience of the feature a wall of retry warnings.
    """
    return min(2, max_workers())


def worker_choices(share: float = DEFAULT_CPU_SHARE) -> list[int]:
    """Selectable worker counts; ``1`` means sequential, with no rate-limit risk."""
    return list(range(1, max_workers(share) + 1))


def describe_workers(workers: int) -> str:
    if workers <= 1:
        return "sequential (one request at a time)"
    return f"{workers} parallel requests"


# ---------------------------------------------------------------------------
# Classifying failures
# ---------------------------------------------------------------------------

#: Earth Engine is busy or we are going too fast: wait and retry the same request.
RATE_LIMIT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "quota",
    "too many concurrent",
    "concurrent aggregation",
    "resource_exhausted",
    "try again later",
)

#: Transient server-side trouble: also worth retrying unchanged.
TRANSIENT_MARKERS = (
    "500",
    "502",
    "503",
    "504",
    "backend error",
    "internal error",
    "deadline exceeded",
    "connection reset",
    "connection aborted",
    "timed out",
    "temporarily unavailable",
)

#: The request itself is too big: retrying will not help, it has to be subdivided.
TOO_LARGE_MARKERS = (
    "too many values",
    "user memory limit exceeded",
    "over 5000 elements",
    "5000 elements",
    "payload size",
    "request payload",
    "too many pixels",
    "image too large",
)


def _matches(error: BaseException | str, markers: Sequence[str]) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in markers)


def is_rate_limited(error: BaseException | str) -> bool:
    return _matches(error, RATE_LIMIT_MARKERS)


def is_transient(error: BaseException | str) -> bool:
    return _matches(error, TRANSIENT_MARKERS)


def is_too_large(error: BaseException | str) -> bool:
    """Should this request be split rather than retried?

    Checked before the transient markers, because "computation timed out" can mean
    either, and treating an over-large request as transient just burns retries.
    """
    return _matches(error, TOO_LARGE_MARKERS)


def should_retry(error: BaseException | str) -> bool:
    if is_too_large(error):
        return False
    return is_rate_limited(error) or is_transient(error)


def should_subdivide(error: BaseException | str) -> bool:
    return is_too_large(error)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter."""

    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.3

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based).

        Jitter matters with several workers: without it they all back off by the
        same amount and hit the server again in lockstep.
        """
        if attempt < 1:
            return 0.0
        delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        if self.jitter:
            rng = rng or random
            delay *= 1.0 + rng.uniform(-self.jitter, self.jitter)
        return max(0.0, delay)


# ---------------------------------------------------------------------------
# Adaptive concurrency
# ---------------------------------------------------------------------------


class AdaptiveGate:
    """Limits requests in flight, and narrows itself when the server pushes back.

    Rate limiting is a property of the *account*, not of one request, so the right
    response to a 429 is for every worker to ease off, not just the one that saw
    it.  :meth:`penalise` halves the allowance and sets a shared pause that all
    workers observe; :meth:`reward` widens it again after a run of successes.
    """

    def __init__(
        self,
        limit: int,
        ceiling: int | None = None,
        floor: int = 1,
        recover_after: int = 5,
    ):
        self._ceiling = max(1, ceiling if ceiling is not None else limit)
        self._floor = max(1, min(floor, self._ceiling))
        self._limit = max(self._floor, min(limit, self._ceiling))
        self._in_flight = 0
        self._successes = 0
        self._recover_after = max(1, recover_after)
        self._paused_until = 0.0
        self._condition = threading.Condition()
        self.penalties = 0

    @property
    def limit(self) -> int:
        with self._condition:
            return self._limit

    @property
    def paused_for(self) -> float:
        with self._condition:
            return max(0.0, self._paused_until - time.monotonic())

    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._in_flight >= self._limit:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                timed_out = not self._condition.wait(remaining)
                if timed_out and deadline is not None and time.monotonic() >= deadline:
                    return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._condition:
            self._in_flight = max(0, self._in_flight - 1)
            self._condition.notify()

    def penalise(self, pause_seconds: float = 0.0) -> int:
        """Halve the allowance and pause everyone. Returns the new limit."""
        with self._condition:
            self._limit = max(self._floor, self._limit // 2)
            self._successes = 0
            self.penalties += 1
            if pause_seconds > 0:
                self._paused_until = max(
                    self._paused_until, time.monotonic() + pause_seconds
                )
            return self._limit

    def reward(self) -> int:
        with self._condition:
            self._successes += 1
            if self._successes >= self._recover_after and self._limit < self._ceiling:
                self._limit += 1
                self._successes = 0
                self._condition.notify()
            return self._limit

    def wait_if_paused(self, sleep: Callable[[float], None] = time.sleep) -> float:
        waited = self.paused_for
        if waited > 0:
            sleep(waited)
        return waited


# ---------------------------------------------------------------------------
# Running jobs
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What happened to one job."""

    key: object
    result: object = None
    error: BaseException | None = None
    attempts: int = 0
    seconds: float = 0.0
    #: Set when the job failed because it was too large to answer at all.
    needs_subdivision: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class RunReport:
    """Summary of a whole run."""

    outcomes: list[Outcome] = field(default_factory=list)
    cancelled: bool = False
    elapsed: float = 0.0
    workers: int = 1
    rate_limit_events: int = 0

    @property
    def succeeded(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def oversized(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.needs_subdivision]

    @property
    def total_attempts(self) -> int:
        return sum(o.attempts for o in self.outcomes)

    @property
    def mean_seconds(self) -> float:
        done = [o.seconds for o in self.outcomes if o.ok and o.seconds > 0]
        return sum(done) / len(done) if done else 0.0

    def summary(self) -> str:
        parts = [
            f"{len(self.succeeded)} of {len(self.outcomes)} succeeded",
            f"{self.elapsed:.1f}s elapsed",
        ]
        if self.workers > 1:
            parts.append(f"{self.workers} workers")
        if self.rate_limit_events:
            parts.append(f"{self.rate_limit_events} rate-limit backoffs")
        if self.oversized:
            parts.append(f"{len(self.oversized)} need subdividing")
        if self.cancelled:
            parts.append("cancelled")
        return ", ".join(parts)


def run_jobs(
    jobs: Sequence,
    work: Callable[[object], object],
    workers: int = 1,
    retry: RetryPolicy | None = None,
    progress: Callable[[Outcome, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> RunReport:
    """Run ``work`` over ``jobs``, retrying what is worth retrying.

    ``workers == 1`` runs strictly sequentially with no thread pool at all, which
    is the safe default: one request in flight can never be rate limited for
    concurrency, and there is nothing to reason about if something goes wrong.

    ``progress`` is called from a worker thread as each job settles, with the
    outcome and the done/total counts.  ``is_cancelled`` is polled between jobs
    and between retries, so a long job stops promptly.
    """
    retry = retry or RetryPolicy()
    workers = max(1, int(workers))
    total = len(jobs)
    report = RunReport(workers=workers)
    if total == 0:
        return report

    gate = AdaptiveGate(limit=workers, ceiling=workers)
    counter = {"done": 0}
    lock = threading.Lock()
    # perf_counter, not monotonic: monotonic's granularity on Windows is about
    # 16 ms, which reads a fast request as having taken no time at all.
    started = time.perf_counter()

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def attempt(job) -> Outcome:
        outcome = Outcome(key=job)
        for number in range(1, retry.attempts + 1):
            if cancelled():
                outcome.error = outcome.error or InterruptedError("Cancelled")
                return outcome
            gate.wait_if_paused(sleep)
            if not gate.acquire():
                continue
            outcome.attempts = number
            begin = time.perf_counter()
            try:
                outcome.result = work(job)
            except Exception as error:
                outcome.error = error
                if should_subdivide(error):
                    outcome.needs_subdivision = True
                    return outcome
                if not should_retry(error) or number >= retry.attempts:
                    return outcome
                if is_rate_limited(error):
                    with lock:
                        report.rate_limit_events += 1
                    delay = retry.delay_for(number, rng)
                    gate.penalise(pause_seconds=delay)
                else:
                    sleep(retry.delay_for(number, rng))
            else:
                outcome.error = None
                outcome.seconds = time.perf_counter() - begin
                gate.reward()
                return outcome
            finally:
                gate.release()
        return outcome

    def settle(outcome: Outcome) -> None:
        with lock:
            counter["done"] += 1
            report.outcomes.append(outcome)
            done = counter["done"]
        if progress:
            progress(outcome, done, total)

    if workers == 1:
        for job in jobs:
            if cancelled():
                report.cancelled = True
                break
            settle(attempt(job))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(attempt, job) for job in jobs]
            for future in futures:
                try:
                    settle(future.result())
                except Exception as error:
                    settle(Outcome(key=None, error=error))
            if cancelled():
                report.cancelled = True

    report.elapsed = time.perf_counter() - started
    if cancelled():
        report.cancelled = True
    return report


# ---------------------------------------------------------------------------
# Predicting wall-clock time
# ---------------------------------------------------------------------------

#: A getRegion round trip, measured loosely.  Refined from real timings as a job
#: runs, so the estimate shown to the user converges on the truth.
DEFAULT_SECONDS_PER_REQUEST = 2.0

#: Parallel speed-up is sublinear: the server serialises some work and there is
#: per-request overhead we cannot hide.
PARALLEL_EFFICIENCY = 0.8


def estimate_seconds(
    jobs: int,
    workers: int = 1,
    seconds_per_request: float = DEFAULT_SECONDS_PER_REQUEST,
    efficiency: float = PARALLEL_EFFICIENCY,
) -> float:
    """Predicted wall-clock seconds for ``jobs`` requests over ``workers``."""
    if jobs <= 0:
        return 0.0
    workers = max(1, int(workers))
    sequential = jobs * max(0.0, seconds_per_request)
    if workers == 1:
        return sequential
    return sequential / (workers * efficiency)


def speedup(
    jobs: int,
    workers: int,
    seconds_per_request: float = DEFAULT_SECONDS_PER_REQUEST,
) -> float:
    """How many times faster than sequential ``workers`` is predicted to be."""
    one = estimate_seconds(jobs, 1, seconds_per_request)
    many = estimate_seconds(jobs, workers, seconds_per_request)
    return one / many if many > 0 else 1.0


def format_duration(seconds: float) -> str:
    """``95`` -> ``1 min 35 s``; for showing an estimate or an elapsed time."""
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return "under a second"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, remainder = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {remainder} s" if remainder else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def measure(outcomes: Iterable[Outcome]) -> float:
    """Mean seconds per successful request, for refining the estimate."""
    times = [o.seconds for o in outcomes if o.ok and o.seconds > 0]
    return sum(times) / len(times) if times else DEFAULT_SECONDS_PER_REQUEST
