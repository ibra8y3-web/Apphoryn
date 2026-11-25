"""Shared pacing for safe, steady catalog import throughput."""
from __future__ import annotations

import os
import time
from collections.abc import Callable


class ImportPacer:
    """Reserve import starts no more frequently than the configured interval.

    The first import starts immediately. Later imports wait until one full interval
    has elapsed since the previous import started. A deadline lets long-running
    workers stop cleanly and persist their remaining queue.
    """

    def __init__(
        self,
        interval_seconds: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds is None:
            try:
                interval_seconds = float(os.getenv("APPHORYN_IMPORT_INTERVAL_SECONDS", "60"))
            except (TypeError, ValueError):
                interval_seconds = 60.0
        self.interval_seconds = max(0.0, min(float(interval_seconds), 3600.0))
        self._clock = clock
        self._sleep = sleeper
        self._last_started_at: float | None = None

    def wait_for_slot(self, deadline: float | None = None) -> bool:
        """Wait for an import slot; return False if the supplied deadline expires."""
        while self._last_started_at is not None:
            remaining = self.interval_seconds - (self._clock() - self._last_started_at)
            if remaining <= 0:
                break
            now = self._clock()
            if deadline is not None and now >= deadline:
                return False
            sleep_for = min(remaining, 1.0)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - now))
            if sleep_for <= 0:
                return False
            self._sleep(sleep_for)

        if deadline is not None and self._clock() >= deadline:
            return False
        self._last_started_at = self._clock()
        return True
