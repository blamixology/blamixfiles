"""Speed limits: one token bucket per direction, shared by all transfers."""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """rate = bytes per second (0 = unlimited). Can be changed while transfers run."""

    def __init__(self, rate: int = 0):
        self._lock = threading.Lock()
        self._rate = 0
        self._tokens = 0.0
        self._last = time.monotonic()
        self.rate = rate

    @property
    def rate(self) -> int:
        return self._rate

    @rate.setter
    def rate(self, value: int) -> None:
        with self._lock:
            self._rate = max(0, int(value or 0))
            self._tokens = min(self._tokens, self._burst())
            self._last = time.monotonic()

    def _burst(self) -> float:
        return max(self._rate / 4, 16 * 1024)      # up to 0.25 s worth at once

    def consume(self, n: int, should_stop: Callable[[], bool] = lambda: False) -> None:
        """Account for n bytes just moved; sleep as long as needed to stay under the rate."""
        while True:
            with self._lock:
                if self._rate <= 0:
                    return
                now = time.monotonic()
                self._tokens = min(self._burst(), self._tokens + (now - self._last) * self._rate)
                self._last = now
                self._tokens -= n
                if self._tokens >= 0:
                    return
                wait = -self._tokens / self._rate
                n = 0                               # already charged; now just wait for the debt
            if should_stop():
                return
            time.sleep(min(wait, 0.2))              # short naps so cancel stays responsive
