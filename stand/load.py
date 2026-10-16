"""Load on the stand's API through its nginx, the way a deployed site gets it.

  python3 stand/load.py [--base http://localhost:8081] [--users 50] [--seconds 30] [--sse 30]

Each simulated user loops over what the UI does: list (with a random sort, filter or search), a game card,
its reviews, the top. `--sse` keeps that many DevPanel event streams open meanwhile.
Prints latency percentiles and error counts per endpoint; exit code 1 on any 5xx or connection error.
Needs httpx (backend/.venv/bin/python stand/load.py).
"""

import argparse
import asyncio
import random
import statistics
import time
from collections import defaultdict

import httpx

SORTS = ["metascore", "userscore", "release", "reviews"]
WORDS = ["star", "dragon", "the", "%", "zzzz", "kingdom", "toy"]


async def user(c: httpx.AsyncClient, slugs: list[str], until: float, lat: dict, errs: dict, rng: random.Random):
    while time.monotonic() < until:
        slug = rng.choice(slugs)
        params = {"sort": rng.choice(SORTS), "limit": 48, "offset": rng.choice([0, 48, 96])}
        if rng.random() < 0.3:
            params["q"] = rng.choice(WORDS)
        for name, path, p in [("list", "/api/games", params), ("card", f"/api/games/{slug}", None),
                              ("reviews", f"/api/games/{slug}/reviews", {"kind": rng.choice(["critic", "user"])}),
                              ("top", "/api/games", {"top": "true", "limit": 20})]:
            t0 = time.monotonic()
            try:
                r = await c.get(path, params=p)
                if r.status_code >= 500:
                    errs[name][r.status_code] += 1
            except httpx.HTTPError as e:
                errs[name][type(e).__name__] += 1
                continue
            lat[name].append((time.monotonic() - t0) * 1000)


async def stream(base: str, until: float, got: list):
    try:
        async with httpx.AsyncClient(base_url=base, timeout=None) as c, c.stream("GET", "/api/events/stream") as r:
            got.append(r.status_code)
            async for _ in r.aiter_bytes():
                if time.monotonic() > until:
                    break
    except httpx.HTTPError as e:
        got.append(type(e).__name__)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8081")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--sse", type=int, default=30)
    a = ap.parse_args()
    limits = httpx.Limits(max_connections=a.users * 2, max_keepalive_connections=a.users)
    async with httpx.AsyncClient(base_url=a.base, timeout=30, limits=limits) as c:
        slugs = [g["slug"] for g in (await c.get("/api/games", params={"limit": 200})).json()["items"]]
        until = time.monotonic() + a.seconds
        lat, errs, streams = defaultdict(list), defaultdict(lambda: defaultdict(int)), []
        t0 = time.monotonic()
        await asyncio.gather(*(user(c, slugs, until, lat, errs, random.Random(i)) for i in range(a.users)),
                             *(stream(a.base, until, streams) for _ in range(a.sse)))
        took = time.monotonic() - t0
    total = sum(len(v) for v in lat.values())
    print(f"{a.users} users, {a.sse} event streams, {took:.0f}s: {total} requests, {total / took:.0f} req/s")
    for name, v in lat.items():
        q = statistics.quantiles(v, n=100) if len(v) > 1 else v * 99
        print(f"  {name:8} n={len(v):5}  p50={q[49]:6.0f} ms  p95={q[94]:6.0f} ms  max={max(v):6.0f} ms"
              f"  errors={dict(errs[name]) or 0}")
    bad_streams = [s for s in streams if s != 200]
    print(f"  streams: {len(streams) - len(bad_streams)} ok, failed: {bad_streams or 0}")
    return 1 if any(errs.values()) or bad_streams else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
