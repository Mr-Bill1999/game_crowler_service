"""HTTP client for Metacritic: one shared rate limit, retries with backoff, an event per request."""

import asyncio
import random
import time
from typing import Any

import httpx

from app.core import faults, tape
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
        last_status: int | None = None
        reason = "no response"
        for attempt in range(1, self.s.max_attempts + 1):
            t0 = time.perf_counter()
            r: httpx.Response | None = None
            try:
                body, cache = await self._attempt(stage, path, params)
                await self.bus.emit(stage, "ok", slug=slug, http=200, ms=round((time.perf_counter() - t0) * 1000),
                                    attempt=attempt, cache=cache)
                return body
            except _BadStatus as e:
                r, reason = e.response, f"http {e.response.status_code}"
            except _BadBody as e:
                reason = str(e)  # a 200 that is not our JSON: a Cloudflare page or a changed API; worth one more try
            except httpx.TransportError as e:
                reason = type(e).__name__
            ms = round((time.perf_counter() - t0) * 1000)
            last_status = r.status_code if r is not None else None
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

        await self.bus.emit(stage, "error", slug=slug, http=last_status, error=f"attempts exhausted: {reason}",
                            path=path)
        raise FetchError(path, last_status, f"attempts exhausted: {reason}")

    async def _attempt(self, stage: str, path: str, params: dict[str, Any] | None) -> tuple[dict, str | None]:
        """One try: an injected fault (test stand), the tape, or the network. Only good answers are recorded."""
        if kind := faults.pick("metacritic", stage):
            return _json_of(faults.http_response(kind, f"{self.s.mc_backend}{path}")), "fault"
        hit = {"cache": "tape"}

        async def live() -> dict:
            hit["cache"] = None
            await self.limiter.acquire()
            async with self.sem:
                r = await self.http.get(path, params=params)
            hit["cache"] = r.headers.get("cf-cache-status")
            return _json_of(r)

        body = await tape.call("metacritic", {"path": path, "params": params or {}}, live, summary=path)
        return body, hit["cache"]


class _BadStatus(Exception):
    def __init__(self, response: httpx.Response):
        super().__init__(f"http {response.status_code}")
        self.response = response


class _BadBody(Exception):
    pass


def _json_of(r: httpx.Response) -> dict:
    if r.status_code != 200:
        raise _BadStatus(r)
    try:
        body = r.json()
    except ValueError:
        raise _BadBody(f"not JSON ({r.headers.get('content-type', '?')})") from None
    if not isinstance(body, dict) or "data" not in body and "components" not in body:
        raise _BadBody("unexpected JSON shape")
    return body
