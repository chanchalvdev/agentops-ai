"""Per-tenant token-bucket rate limiting (product.txt 16).

Each (tenant, route class) has a bucket of `per_minute` tokens refilled continuously; a request
takes one token or is refused with 429 and a Retry-After header. Buckets live in process memory:
exact for the single API process this project deploys, but replicas behind a load balancer would
each allow the full rate. A shared store (Redis) is the documented upgrade path, not done here.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, limits: dict[str, int], clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits  # route class -> requests per minute
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self.rejected = 0

    def acquire(self, tenant_id: str, route: str) -> float | None:
        """Take a token. Returns None if allowed, else seconds until one is available."""
        per_minute = self.limits[route]
        rate = per_minute / 60.0
        now = self._clock()
        bucket = self._buckets.setdefault((tenant_id, route), _Bucket(float(per_minute), now))
        bucket.tokens = min(per_minute, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        self.rejected += 1
        return (1 - bucket.tokens) / rate
