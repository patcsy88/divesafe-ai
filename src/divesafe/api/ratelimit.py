"""Per-actor request limits: a sliding window per (limit key, actor).

In-process only: with several workers each has its own counters, so the effective limit is
multiplied. These are operational limits against abuse and upstream quota exhaustion, not safety
limits. A denied request is not counted.

Counters for a name expire with their window; in development, where the actor name comes from a
request header, an attacker can still create live counters faster than they expire.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MAX_TRACKED = 10_000
LIMIT_KEYS = frozenset({"create", "decide", "read"})


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.requests <= 0 or self.window_seconds <= 0:
            raise ValueError("a limit needs positive requests and window")


class RateLimiter:
    def __init__(
        self,
        limits: Mapping[str, Limit],
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self._limits = dict(limits)
        self._clock = clock
        self._max_tracked = max_tracked
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, limit_key: str, actor: str) -> int | None:
        """Count this request. Returns None if allowed, else whole seconds to wait."""
        limit = self._limits[limit_key]  # an unknown key is a programming error, not a bypass
        with self._lock:
            now = self._clock()
            hits = self._hits.setdefault((limit_key, actor), deque())
            while hits and now - hits[0] >= limit.window_seconds:
                hits.popleft()
            if len(hits) >= limit.requests:
                return max(1, math.ceil(limit.window_seconds - (now - hits[0])))
            hits.append(now)
            if len(self._hits) > self._max_tracked:
                self._evict(now)
            return None

    def _evict(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or self._expired(k, h, now)]:
            del self._hits[key]

    def _expired(self, key: tuple[str, str], hits: deque[float], now: float) -> bool:
        return now - hits[-1] >= self._limits[key[0]].window_seconds
