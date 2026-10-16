"""Local protection for provider limits: a short response cache, a per-host minimum interval and
combined quota budgets.

Provider limits (verified in docs/data-sources.md): data.gov.my 4 requests/minute; Open-Meteo free
tier 600 calls/minute, 5,000/hour and 10,000/day (its terms page, 2026-10-09). The two Open-Meteo
hosts are counted TOGETHER, conservatively, because we could not verify how the provider counts
them. Cache hits cost nothing. When a call would break the interval or exhaust a budget, and
nothing is cached, it fails with `ConnectorTransportError`, so the category is missing and the
assessment is INSUFFICIENT EVIDENCE. It fails closed and never serves old data beyond the cache
TTL. The budgets are per process (several workers each get their own) and are not per user.
"""

from __future__ import annotations

import copy
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from divesafe.data.errors import ConnectorTransportError
from divesafe.data.http import JsonGetter

DEFAULT_MIN_INTERVAL_SECONDS: Mapping[str, float] = {
    "api.data.gov.my": 0.0,
    "marine-api.open-meteo.com": 0.5,
    "api.open-meteo.com": 0.5,
}
MAX_CACHE_ENTRIES = 256


@dataclass(frozen=True)
class QuotaGroup:
    """Hosts that share a budget, and the (window seconds, maximum calls) limits that apply."""

    name: str
    hosts: frozenset[str]
    limits: tuple[tuple[float, int], ...]
    path_prefix: str = ""


OPEN_METEO_QUOTA = QuotaGroup(
    name="open-meteo",
    hosts=frozenset({"marine-api.open-meteo.com", "api.open-meteo.com"}),
    limits=((60.0, 600), (3600.0, 5000), (86400.0, 10000)),
)
# developer.data.gov.my/rate-limit (checked 2026-10-11): the Weather API allows 4 requests per
# minute, and the forecast and warning endpoints share it, so it is a budget, not a spacing.
DATA_GOV_MY_QUOTA = QuotaGroup(
    name="data-gov-my",
    hosts=frozenset({"api.data.gov.my"}),
    limits=((60.0, 4),),
)
# The optional forecast may use at most one of those four, so it cannot starve the warnings feed.
DATA_GOV_MY_FORECAST_QUOTA = QuotaGroup(
    name="data-gov-my-forecast",
    hosts=frozenset({"api.data.gov.my"}),
    limits=((60.0, 1),),
    path_prefix="/weather/forecast",
)
# MET developer guide (read 2026-10-16): 10 a minute and 2,000 a day. Its public page says 3 and
# 1,000. The stricter pair is used because the two pages disagree.
MET_API_QUOTA = QuotaGroup(
    name="met-api",
    hosts=frozenset({"api.met.gov.my"}),
    limits=((60.0, 3), (86400.0, 1000)),
)
DEFAULT_QUOTAS: tuple[QuotaGroup, ...] = (
    OPEN_METEO_QUOTA,
    DATA_GOV_MY_QUOTA,
    DATA_GOV_MY_FORECAST_QUOTA,
    MET_API_QUOTA,
)


class CachingRateLimitedGetter:
    def __init__(
        self,
        inner: JsonGetter,
        *,
        min_interval_seconds: Mapping[str, float] = DEFAULT_MIN_INTERVAL_SECONDS,
        cache_ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        quotas: tuple[QuotaGroup, ...] = DEFAULT_QUOTAS,
    ) -> None:
        self._inner = inner
        self._min = dict(min_interval_seconds)
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, Any]] = {}
        self._last_call: dict[str, float] = {}
        self._quotas = quotas
        self._spent: dict[str, deque[float]] = {q.name: deque() for q in quotas}

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
        self._reserve_quota(host, urllib.parse.urlsplit(url).path, now)
        self._last_call[host] = now

        payload = await self._inner.get_json(url, params)
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]))
        self._cache[key] = (now, copy.deepcopy(payload))
        return payload

    def _reserve_quota(self, host: str, path: str, now: float) -> None:
        applicable = [g for g in self._quotas if host in g.hosts and path.startswith(g.path_prefix)]
        # Check every group before spending from any, so a refusal costs nothing anywhere.
        for group in applicable:
            spent = self._spent[group.name]
            longest = max(window for window, _ in group.limits)
            while spent and now - spent[0] > longest:
                spent.popleft()
            for window, maximum in group.limits:
                used = sum(1 for t in spent if now - t <= window)
                if used >= maximum:
                    raise ConnectorTransportError(
                        f"local call budget for {group.name} is used up; try again later"
                    )
        for group in applicable:
            self._spent[group.name].append(now)
