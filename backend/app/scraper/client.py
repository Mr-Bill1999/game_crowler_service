"""HTTP client for Metacritic: one shared rate limit, retries with backoff, an event per request."""

import asyncio
import random
import time
from typing import Any

import httpx

from app.core import tape
from app.core.config import Settings
from app.events.bus import EventBus

RETRYABLE = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, reason: str):
        super().__init__(f"{status} {reason}: {url}")
        self.status = status


class RateLimiter:
    """Spaces requests evenly: at most `rps` request starts per second across all tasks."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


class MCClient:
    def __init__(self, settings: Settings, bus: EventBus, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.bus = bus
        self.limiter = RateLimiter(settings.rate_limit_rps)
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self.http = http or httpx.AsyncClient(
            base_url=settings.mc_backend,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=settings.http_timeout_s,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def get_json(self, stage: str, path: str, params: dict[str, Any] | None = None,
                       slug: str | None = None) -> dict:
        async def on_hit() -> None:
            await self.bus.emit(stage, "ok", slug=slug, http=200, ms=0, cache="tape")

        return await tape.call("metacritic", {"path": path, "params": params or {}},
                               lambda: self._get_json_live(stage, path, params, slug), summary=path, on_hit=on_hit)

    async def _get_json_live(self, stage: str, path: str, params: dict[str, Any] | None,
                             slug: str | None) -> dict:
        last_status: int | None = None
        for attempt in range(1, self.s.max_attempts + 1):
            await self.limiter.acquire()
            r, err = None, "no response"
            t0 = time.perf_counter()
            async with self.sem:
                try:
                    r = await self.http.get(path, params=params)
                except httpx.TransportError as e:
                    r, err = None, type(e).__name__
            ms = round((time.perf_counter() - t0) * 1000)

            if r is not None and r.status_code == 200:
                await self.bus.emit(stage, "ok", slug=slug, http=200, ms=ms, bytes=len(r.content),
                                    attempt=attempt, cache=r.headers.get("cf-cache-status"))
                return r.json()

            last_status = r.status_code if r is not None else None
            reason = err if r is None else f"http {r.status_code}"
            if r is not None and r.status_code not in RETRYABLE:
                await self.bus.emit(stage, "error", slug=slug, http=r.status_code, ms=ms,
                                    attempt=attempt, error=reason, path=path)
                raise FetchError(path, r.status_code, reason)

            delay = min(60.0, 2 ** attempt) + random.uniform(0, 1)
            if r is not None and (retry_after := r.headers.get("retry-after", "")).isdigit():
                delay = max(delay, float(retry_after))
            await self.bus.emit(stage, "retry", slug=slug, http=last_status, ms=ms,
                                attempt=attempt, error=reason, retry_in_s=round(delay, 1))
            await asyncio.sleep(delay)

        await self.bus.emit(stage, "error", slug=slug, http=last_status, error="attempts exhausted", path=path)
        raise FetchError(path, last_status, "attempts exhausted")
