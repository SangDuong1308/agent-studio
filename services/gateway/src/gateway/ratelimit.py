"""Small in-process sliding-window rate limiter.

Protects the endpoints that cost money or can be brute-forced (login, register,
agent creation, queries). State lives in memory, so with several gateway
replicas each keeps its own counters. That is still a useful brake; move the
counters to Redis when a single global limit matters.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from common.settings import get_settings
from fastapi import HTTPException, Request, status

_MAX_TRACKED_KEYS = 10_000


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: float) -> int | None:
        """Record a hit. Return seconds to wait if over the limit, else None."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, math.ceil(hits[0] + window_seconds - now))
        hits.append(now)
        if len(self._hits) > _MAX_TRACKED_KEYS:
            self._prune(now, window_seconds)
        return None

    def _prune(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def enforce(key: str, limit: int, window_seconds: float) -> None:
    """Raise 429 with a Retry-After header when ``key`` exceeds its budget."""
    if not get_settings().rate_limit_enabled:
        return
    retry_after = limiter.check(key, limit, window_seconds)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Please slow down.",
            headers={"Retry-After": str(retry_after)},
        )


def client_ip(request: Request) -> str:
    # Behind a reverse proxy this is the proxy's address. Configure uvicorn's
    # --proxy-headers / trusted hosts before relying on X-Forwarded-For.
    return request.client.host if request.client else "unknown"
