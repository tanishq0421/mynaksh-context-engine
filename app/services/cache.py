"""In-memory TTL cache with stale retention and single-flight.

Three behaviours, each earning its place:

1. **TTL** — per-service, because the data ages at different rates (see
   ServicePolicy).
2. **Stale retention** — an expired entry is kept, not evicted, so that when an
   upstream is down we can answer with yesterday's panchang instead of nothing.
   This is the whole difference between *degraded* and *broken*.
3. **Single-flight** — when a TTL expires under concurrent load, one caller
   refreshes and the rest await that same future. Without it, N simultaneous
   requests become N identical upstream calls at exactly the moment the upstream
   is least likely to be healthy.

No locking anywhere below, and that is deliberate rather than an oversight:
asyncio runs one coroutine at a time on one thread, so any stretch of code
without an `await` is already atomic. The `in_flight` dict is read and written
only in such stretches.

This cache is per-process. Production would put a shared Redis behind the same
interface and run several workers; in-memory means this deployment is a single
worker, since two workers would each keep their own copy and halve the hit rate.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


def cache_key(service: str, identifier: str | None) -> str:
    """`service:identifier`.

    Panchang takes no user id — callers pass the date instead, so the global
    payload is shared by every user and still rolls over at midnight.
    """
    return f"{service}:{identifier or '_'}"


@dataclass
class _Entry:
    value: Any
    expires_at: float

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at


class TTLCache:
    """Async TTL cache. Not thread-safe, and does not need to be."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        # key -> the refresh currently in progress, for single-flight.
        self._in_flight: dict[str, asyncio.Future[Any]] = {}

    # -- reads ------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Fresh value, or None if missing or expired."""
        entry = self._entries.get(key)
        if entry is None or not entry.is_fresh(time.monotonic()):
            return None
        return entry.value

    def get_stale(self, key: str) -> Any | None:
        """Last known good value regardless of age.

        Only called after an upstream has already failed — at that point an old
        answer beats no answer, and the caller flags it `stale=True` so the
        response never pretends it is current.
        """
        entry = self._entries.get(key)
        return entry.value if entry is not None else None

    def has_stale(self, key: str) -> bool:
        return key in self._entries

    # -- writes -----------------------------------------------------------

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        """Drop everything, including stale entries. Wired to the dev-only
        cache-flush endpoint so a demo can force the cold path."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # -- single-flight ----------------------------------------------------

    async def get_or_fetch(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Return `(value, was_cached)`, running `loader` at most once per key.

        A concurrent caller that arrives while a refresh is in flight awaits the
        same future rather than starting a second one. It is reported as *not*
        cached, because it did participate in a live fetch — that keeps the
        "cached" flag meaning "no upstream was involved".
        """
        hit = self.get(key)
        if hit is not None:
            return hit, True

        in_flight = self._in_flight.get(key)
        if in_flight is not None:
            return await in_flight, False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._in_flight[key] = future
        try:
            value = await loader()
        except BaseException as exc:  # noqa: BLE001 - re-raised below, see comment
            # Followers must observe the same outcome as the leader, otherwise a
            # failed refresh would leave them awaiting a future nobody resolves.
            self._in_flight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
            # Nobody may ever await this future (the leader is the only certain
            # consumer), and an un-retrieved exception logs a warning at GC.
            future.exception()
            raise
        else:
            self._in_flight.pop(key, None)
            if not future.done():
                future.set_result(value)
            self.set(key, value, ttl_seconds)
            return value, False
