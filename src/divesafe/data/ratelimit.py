"""Local protection for provider limits: a short response cache and a per-host minimum interval.

Provider limits (verified in docs/data-sources.md): data.gov.my 4 requests/minute, Open-Meteo
free tier 600/minute and 10,000/day. When a call would break the interval and nothing is cached,
the call fails with `ConnectorTransportError`, so the category is missing and the assessment is
INSUFFICIENT EVIDENCE. It fails closed; it never serves old data beyond the cache TTL.
"""

from __future__ import annotations

import copy
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from divesafe.data.errors import ConnectorTransportError
from divesafe.data.http import JsonGetter

DEFAULT_MIN_INTERVAL_SECONDS: Mapping[str, float] = {
    "api.data.gov.my": 15.0,
    "marine-api.open-meteo.com": 0.5,
}
MAX_CACHE_ENTRIES = 256


class CachingRateLimitedGetter:
    def __init__(
        self,
        inner: JsonGetter,
        *,
        min_interval_seconds: Mapping[str, float] = DEFAULT_MIN_INTERVAL_SECONDS,
        cache_ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._min = dict(min_interval_seconds)
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
        self._last_call: dict[str, float] = {}

    async def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        key = (url, tuple(sorted(params.items())))
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] <= self._ttl:
            return copy.deepcopy(hit[1])

        host = urllib.parse.urlsplit(url).hostname or ""
        last = self._last_call.get(host)
        if last is not None and now - last < self._min.get(host, 0.0):
            raise ConnectorTransportError(f"local rate limit for {host}; try again shortly")
        self._last_call[host] = now

        payload = await self._inner.get_json(url, params)
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]))
        self._cache[key] = (now, copy.deepcopy(payload))
        return payload
