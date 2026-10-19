"""HTTP client for the platforms besides Metacritic: a pace per host, retries with backoff, tape and faults.

Each source has a name ("steam", "critics") that the tape files and GA_FAULTS use, as "metacritic" and "youtube" do.
"""

import asyncio
import random
import time
from typing import Any

import httpx

from app.core import faults, tape

RETRYABLE = {429, 500, 502, 503, 504}
UA_BOT = "GameAnalytics/1.0 (+https://github.com/Mr-Bill1999/game_crowler_service)"
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")


class WebError(Exception):
    def __init__(self, url: str, status: int | None, reason: str):
        super().__init__(f"{status or ''} {reason}: {url}".strip())
        self.status = status


class WebClient:
    def __init__(self, source: str, rps: float, *, attempts: int = 4, timeout: float = 30,
                 http: httpx.AsyncClient | None = None):
        self.source = source
        self.interval = 1.0 / rps
        self.attempts = attempts
        self.http = http or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _pace(self, host: str) -> None:
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(host, 0.0))
            self._next[host] = at + self.interval
        if at > now:
            await asyncio.sleep(at - now)

    async def _once(self, url: str, params: dict | None, headers: dict, cookies: dict | None) -> httpx.Response:
        if kind := faults.pick(self.source):
            return faults.http_response(kind, url)
        await self._pace(httpx.URL(url).host)
        return await self.http.get(url, params=params, headers=headers, cookies=cookies)

    async def _get(self, url: str, params: dict | None, headers: dict, cookies: dict | None) -> httpx.Response:
        reason, status = "no response", None
        for attempt in range(1, self.attempts + 1):
            try:
                r = await self._once(url, params, headers, cookies)
            except httpx.HTTPError as e:
                reason, status = type(e).__name__, None
            else:
                if r.status_code not in RETRYABLE:
                    return r
                reason, status = f"HTTP {r.status_code}", r.status_code
                after = r.headers.get("retry-after", "").strip()
                if after.isdigit() and attempt < self.attempts:
                    await asyncio.sleep(min(60, int(after)))
                    continue
            if attempt < self.attempts:
                await asyncio.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))
        raise WebError(url, status, reason)

    async def get_json(self, url: str, params: dict | None = None, *, browser: bool = False,
                       cookies: dict | None = None) -> Any:
        async def live():
            r = await self._get(url, params, {"User-Agent": UA_BROWSER if browser else UA_BOT}, cookies)
            if r.status_code != 200:
                raise WebError(url, r.status_code, "not OK")
            try:
                return r.json()
            except ValueError:
                raise WebError(url, r.status_code, "not JSON (a Cloudflare page or a changed API)") from None

        return await tape.call(self.source, {"url": url, "params": params or {}}, live, summary=url[:200])

    async def get_text(self, url: str, params: dict | None = None, *, browser: bool = True,
                       cookies: dict | None = None) -> dict:
        """{"status", "text", "url"}: pages are judged by the caller (a 404 or a paywall is an answer, not a fault)."""
        async def live():
            r = await self._get(url, params, {"User-Agent": UA_BROWSER if browser else UA_BOT}, cookies)
            return {"status": r.status_code, "text": r.text if r.status_code == 200 else "", "url": str(r.url)}

        return await tape.call(self.source, {"url": url, "params": params or {}, "text": True}, live,
                               summary=url[:200])
