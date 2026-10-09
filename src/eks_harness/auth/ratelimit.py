from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    prefix: str
    threshold: int
    window_seconds: float
    base_delay: float = 1.0
    max_delay: float = 60.0

    def delay(self, failures: int) -> float:
        if failures < self.threshold:
            return 0.0
        return min(self.max_delay, self.base_delay * 2 ** (failures - self.threshold))


DEFAULT_LIMITS = (
    Limit("ip:", 20, 600.0),
    Limit("user:", 5, 900.0),
)


class LoginRateLimiter:
    def __init__(self, limits: Iterable[Limit] = DEFAULT_LIMITS, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10000) -> None:
        self.limits = tuple(limits)
        self.clock = clock
        self.max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _limit_for(self, key: str) -> Limit | None:
        for limit in self.limits:
            if key.startswith(limit.prefix):
                return limit
        return None

    def _prune(self, key: str, limit: Limit, at: float) -> deque[float]:
        entries = self._failures.get(key)
        if entries is None:
            return deque()
        while entries and at - entries[0] >= limit.window_seconds:
            entries.popleft()
        if not entries:
            self._failures.pop(key, None)
        return entries

    def retry_after(self, keys: Iterable[str]) -> int | None:
        at = self.clock()
        wait = 0.0
        with self._lock:
            for key in keys:
                limit = self._limit_for(key)
                if limit is None:
                    continue
                entries = self._prune(key, limit, at)
                delay = limit.delay(len(entries))
                if delay > 0:
                    wait = max(wait, entries[-1] + delay - at)
        return max(1, math.ceil(wait)) if wait > 0 else None

    def failure(self, keys: Iterable[str]) -> None:
        at = self.clock()
        with self._lock:
            if len(self._failures) >= self.max_keys:
                self._evict(at)
            for key in keys:
                if self._limit_for(key) is None:
                    continue
                self._failures.setdefault(key, deque()).append(at)

    def success(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                if key.startswith("user:"):
                    self._failures.pop(key, None)

    def _evict(self, at: float) -> None:
        for key in list(self._failures):
            limit = self._limit_for(key)
            if limit is not None:
                self._prune(key, limit, at)
        while len(self._failures) >= self.max_keys:
            self._failures.pop(next(iter(self._failures)))

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


def login_keys(ip: str | None, username: str | None) -> list[str]:
    keys = [f"ip:{ip or 'unknown'}"]
    if username:
        keys.append(f"user:{username.strip().casefold()}")
    return keys
