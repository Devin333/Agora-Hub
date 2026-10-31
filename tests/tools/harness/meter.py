"""Measure what a stress run costs: wall time, traced memory, threads, attempt capacity.

``StressMeter`` takes a sample whenever the test calls ``sample`` (after a
batch of work, so the memory curve can be asserted flat) and, from a
background thread, watches the peak thread count and attempt capacity in
between, which is where concurrent runs peak. Each run prints one line with
the numbers the report quotes.
"""

from __future__ import annotations

import gc
import threading
import time
import tracemalloc
from dataclasses import dataclass, field

from framework.shared.attempts import DEFAULT_ATTEMPT_EXECUTION_CAPACITY


@dataclass
class StressSample:
    units: int
    elapsed_seconds: float
    traced_bytes: int
    threads: int
    attempt_capacity_active: int


PEAK_POLL_SECONDS = 0.01


@dataclass
class StressMeter:
    name: str
    units: int | None = None
    samples: list[StressSample] = field(default_factory=list)
    peak_threads: int = 0
    peak_attempt_capacity: int = 0
    _started: float = 0.0
    _stop: threading.Event = field(default_factory=threading.Event)
    _watcher: threading.Thread | None = None

    def __enter__(self) -> "StressMeter":
        gc.collect()
        tracemalloc.start()
        self._started = time.monotonic()
        self.sample(0)
        self._watcher = threading.Thread(target=self._watch_peaks, name="stress-meter", daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join()
        last_units = self.samples[-1].units if self.samples else 0
        self.sample(self.units if self.units is not None else last_units)
        tracemalloc.stop()
        print(self.report())

    def _watch_peaks(self) -> None:
        # The watcher itself is one of the live threads; it is not counted.
        while not self._stop.wait(PEAK_POLL_SECONDS):
            self.peak_threads = max(self.peak_threads, threading.active_count() - 1)
            self.peak_attempt_capacity = max(
                self.peak_attempt_capacity, DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active
            )

    def sample(self, units: int) -> None:
        gc.collect()
        current, _peak = tracemalloc.get_traced_memory()
        self.samples.append(
            StressSample(
                units=units,
                elapsed_seconds=time.monotonic() - self._started,
                traced_bytes=current,
                threads=threading.active_count(),
                attempt_capacity_active=DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active,
            )
        )

    def memory_growth_bytes(self, *, skip_warmup: int = 1) -> int:
        """Traced memory at the end minus after warm-up (caches and pools fill first)."""

        settled = self.samples[min(skip_warmup, len(self.samples) - 1)]
        return self.samples[-1].traced_bytes - settled.traced_bytes

    def max_threads(self) -> int:
        return max([self.peak_threads, *(sample.threads for sample in self.samples)])

    def report(self) -> str:
        first, last = self.samples[0], self.samples[-1]
        rate = last.units / last.elapsed_seconds if last.elapsed_seconds > 0 else 0.0
        curve = " ".join(f"{sample.traced_bytes // 1024}K" for sample in self.samples)
        return (
            f"[stress] {self.name}: {last.units} units in {last.elapsed_seconds:.2f}s "
            f"({rate:.0f}/s); memory {first.traced_bytes // 1024}K -> {last.traced_bytes // 1024}K "
            f"(growth after warm-up {self.memory_growth_bytes() // 1024}K); "
            f"threads max {self.max_threads()}; attempt slots peak {self.peak_attempt_capacity}, "
            f"at end {last.attempt_capacity_active}; "
            f"curve {curve}"
        )
