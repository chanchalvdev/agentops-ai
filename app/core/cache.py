"""A bounded in-process TTL + LRU cache, used for query embeddings and tool results.

Callers build the key, because only they know what the value depends on: every key must carry the
tenant, the authorization scope, and the versions that make an entry stale (corpus, model,
configuration). Expiry is a backstop; versioned keys are what actually invalidate entries.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class TTLCache[V]:
    def __init__(
        self, max_entries: int, ttl_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max, self._ttl, self._clock = max_entries, ttl_s, clock
        self._items: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()
        self.stats = CacheStats()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Hashable) -> V | None:
        item = self._items.get(key)
        if item is None or item[0] <= self._clock():
            self._items.pop(key, None)
            self.stats.misses += 1
            return None
        self._items.move_to_end(key)
        self.stats.hits += 1
        return item[1]

    def put(self, key: Hashable, value: V) -> None:
        self._items[key] = (self._clock() + self._ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)
            self.stats.evictions += 1
