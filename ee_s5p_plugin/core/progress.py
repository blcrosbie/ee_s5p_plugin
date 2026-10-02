"""Tracking the progress of a long job, and wording it for the user.

A heavy extraction is hundreds of requests over several minutes.  A bare
percentage does not answer the questions people actually have -- is it still
moving, how much is left, how long, is anything going wrong -- so this tracks
enough to answer all four and renders it as one line:

    Tile 143 of 545 · 28,600 records · 2.1 tiles/s · about 3 min left

The remaining time comes from the *measured* rate once there are enough samples,
so it self-corrects instead of repeating an estimate made before the job started.
Early on, when a couple of samples would give a wild answer, it falls back to the
rate the plan predicted.

Pure Python: no Qt, no QGIS.  The GUI reads :meth:`ProgressTracker.snapshot`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .concurrency import format_duration

#: Below this many completed units, a measured rate is too noisy to show an ETA.
MIN_SAMPLES_FOR_RATE = 3

#: Don't redraw more often than this; a 500-tile job would otherwise repaint the
#: dock hundreds of times a second and starve the thread doing the work.
MIN_REDRAW_INTERVAL = 0.2


@dataclass(frozen=True)
class ProgressSnapshot:
    """An immutable view of a job's progress, safe to hand to another thread."""

    phase: str = ""
    done: int = 0
    total: int = 0
    records: int = 0
    failures: int = 0
    throttles: int = 0
    elapsed: float = 0.0
    #: Units per second, measured; 0.0 when there is not enough data yet.
    rate: float = 0.0
    #: Units per second the plan predicted, used until ``rate`` is trustworthy.
    expected_rate: float = 0.0
    unit: str = "tile"
    #: True while throughput cannot be known, e.g. a single opaque request.
    indeterminate: bool = False
    #: Set while every worker is waiting out a rate limit.
    throttled: bool = False
    note: str = ""

    # -- derived ----------------------------------------------------------

    @property
    def percent(self) -> int | None:
        if self.indeterminate or self.total <= 0:
            return None
        return max(0, min(100, int(100.0 * self.done / self.total)))

    @property
    def effective_rate(self) -> float:
        if self.done >= MIN_SAMPLES_FOR_RATE and self.rate > 0:
            return self.rate
        return self.expected_rate

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def remaining_seconds(self) -> float | None:
        if self.indeterminate or self.total <= 0 or self.done >= self.total:
            return None
        rate = self.effective_rate
        if rate <= 0:
            return None
        return self.remaining / rate

    @property
    def finished(self) -> bool:
        return self.total > 0 and self.done >= self.total

    def _plural(self, count: int) -> str:
        return self.unit if count == 1 else f"{self.unit}s"

    # -- wording ----------------------------------------------------------

    def status_line(self) -> str:
        """The one line shown under the progress bar."""
        parts: list[str] = []

        if self.indeterminate:
            parts.append(self.phase or "Working")
            if self.elapsed >= 1:
                parts.append(f"{format_duration(self.elapsed)} elapsed")
        elif self.total > 0:
            parts.append(f"{self.unit.capitalize()} {self.done:,} of {self.total:,}")
        else:
            parts.append(self.phase or "Working")

        if self.records:
            parts.append(f"{self.records:,} records")

        if self.throttled:
            # Say what is happening and that it is handled, not just that it is slow.
            parts.append("rate limited, backing off")
        elif not self.indeterminate and self.effective_rate > 0 and not self.finished:
            parts.append(f"{self._format_rate()}")

        remaining = self.remaining_seconds
        if remaining is not None and remaining >= 1:
            parts.append(f"about {format_duration(remaining)} left")
        elif self.finished:
            parts.append(f"done in {format_duration(self.elapsed)}")

        if self.failures:
            parts.append(f"{self.failures} failed")

        if self.note:
            parts.append(self.note)

        return " · ".join(parts)

    def _format_rate(self) -> str:
        rate = self.effective_rate
        if rate >= 10:
            # A decimal is noise once the rate is this high.
            return f"{rate:,.0f} {self._plural(2)}/s"
        if rate >= 1:
            return f"{rate:.1f} {self._plural(2)}/s"
        per_minute = rate * 60.0
        if per_minute >= 1:
            return f"{per_minute:.0f} {self._plural(2)}/min"
        return f"{per_minute:.1f} {self._plural(2)}/min"

    def bar_format(self) -> str:
        """Text for the progress bar itself; ``%p`` is Qt's percent placeholder."""
        if self.indeterminate:
            return self.phase or "Working…"
        return f"{self.phase or 'Working'} %p%"

    # -- crossing a thread boundary ---------------------------------------

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "done": self.done,
            "total": self.total,
            "records": self.records,
            "failures": self.failures,
            "throttles": self.throttles,
            "elapsed": self.elapsed,
            "rate": self.rate,
            "expected_rate": self.expected_rate,
            "unit": self.unit,
            "indeterminate": self.indeterminate,
            "throttled": self.throttled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ProgressSnapshot:
        known = {f: payload[f] for f in cls.__dataclass_fields__ if f in payload}
        return cls(**known)


@dataclass
class ProgressTracker:
    """Accumulates what a running job has done so far.

    Mutated from a worker thread and read via :meth:`snapshot`, which returns an
    immutable copy -- so the GUI never reads a half-updated state.
    """

    total: int = 0
    phase: str = "Working"
    unit: str = "tile"
    #: Units per second the plan predicted; used for the ETA until measurement is
    #: trustworthy.
    expected_rate: float = 0.0
    indeterminate: bool = False

    done: int = 0
    records: int = 0
    failures: int = 0
    throttles: int = 0
    note: str = ""
    _throttled_until: float = field(default=0.0, repr=False)
    # The lambda defers the lookup to call time, so the clock can be substituted
    # in tests; ``default_factory=time.perf_counter`` would bind the original
    # function at class-creation time and ignore any replacement.
    _started: float = field(default_factory=lambda: time.perf_counter(), repr=False)
    _last_emit: float = field(default=0.0, repr=False)

    # -- updates ----------------------------------------------------------

    def advance(
        self,
        units: int = 1,
        records: int = 0,
        failures: int = 0,
        throttles: int = 0,
    ) -> None:
        self.done += units
        self.records += records
        self.failures += failures
        self.throttles += throttles

    def add_total(self, units: int) -> None:
        """Grow the denominator, e.g. when a refused tile is split into children."""
        self.total += max(0, units)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def set_note(self, note: str) -> None:
        self.note = note

    def mark_throttled(self, seconds: float) -> None:
        """Record that the job is waiting out a rate limit for ``seconds``."""
        if seconds > 0:
            self._throttled_until = time.perf_counter() + seconds

    @property
    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self._started)

    @property
    def throttled(self) -> bool:
        return time.perf_counter() < self._throttled_until

    @property
    def rate(self) -> float:
        elapsed = self.elapsed
        return self.done / elapsed if elapsed > 0 and self.done > 0 else 0.0

    def should_emit(self, interval: float = MIN_REDRAW_INTERVAL) -> bool:
        """Rate-limit redraws, but never skip the first or last update.

        Without this, a 500 tile job repaints the dock as fast as replies arrive,
        which competes with the threads doing the actual work.
        """
        now = time.perf_counter()
        if self.done <= 1 or (self.total and self.done >= self.total):
            self._last_emit = now
            return True
        if now - self._last_emit >= interval:
            self._last_emit = now
            return True
        return False

    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(
            phase=self.phase,
            done=self.done,
            total=self.total,
            records=self.records,
            failures=self.failures,
            throttles=self.throttles,
            elapsed=self.elapsed,
            rate=self.rate,
            expected_rate=self.expected_rate,
            unit=self.unit,
            indeterminate=self.indeterminate,
            throttled=self.throttled,
            note=self.note,
        )


def rate_from_estimate(units: int, seconds: float) -> float:
    """Convert a predicted duration into a units-per-second rate."""
    if units <= 0 or seconds <= 0:
        return 0.0
    return units / seconds
