"""Shared HTTP helpers: rate limiters and the synchronous API client."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx


class MinIntervalLimiter:
    """Blocks so that consecutive calls are at least ``interval`` seconds apart."""

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


# One limiter per API endpoint for the whole process, so separate
# resolver/source instances still share the public server's rate limit.
_limiters: dict[str, MinIntervalLimiter] = {}
_limiters_lock = threading.Lock()


def limiter_for(key: str, interval: float) -> MinIntervalLimiter:
    with _limiters_lock:
        lim = _limiters.get(key)
        if lim is None or lim.interval != interval:
            lim = _limiters[key] = MinIntervalLimiter(interval)
        return lim


class AsyncKeyedLimiter:
    """Per-key (per-domain) minimum interval between requests, for asyncio."""

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, key: str) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            delay = self._last.get(key, -1e9) + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[key] = time.monotonic()


def api_client(user_agent: str, timeout: float = 30.0,
               transport: httpx.BaseTransport | None = None,
               accept: str = "*/*") -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept": accept},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )
