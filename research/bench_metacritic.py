"""Benchmark: async fetch of N Metacritic game cards via backend JSON API.

usage: bench.py <source: new_releases|catalog> <concurrency> [n=20] [page_limit=500]
"""
import asyncio, json, sys, time, statistics
import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
B = "https://backend.metacritic.com"
SRC = {
    "new_releases": f"{B}/finder/metacritic/web?sortBy=-releaseDate&productType=games&metaScoreMin=1&offset={{off}}&limit={{n}}",
    "catalog": f"{B}/finder/metacritic/web?sortBy=-releaseDate&productType=games&offset={{off}}&limit={{n}}",
}

src, conc = sys.argv[1], int(sys.argv[2])
N = int(sys.argv[3]) if len(sys.argv) > 3 else 20
PAGE = int(sys.argv[4]) if len(sys.argv) > 4 else 500
OFF = int(sys.argv[5]) if len(sys.argv) > 5 else 0
sem = asyncio.Semaphore(conc)
lat = {}  # kind -> [seconds]
stats = {"requests": 0, "bytes": 0, "errors": {}}


async def get(c, kind, url):
    async with sem:
        t = time.perf_counter()
        r = await c.get(url)
        dt = time.perf_counter() - t
    lat.setdefault(kind, []).append(dt)
    stats["requests"] += 1
    stats["bytes"] += len(r.content)
    if r.status_code != 200:
        stats["errors"][r.status_code] = stats["errors"].get(r.status_code, 0) + 1
        return None
    return r.json()


async def reviews(c, kind, slug):
    items, off = [], 0
    while True:
        d = await get(c, kind, f"{B}/reviews/metacritic/{kind}/games/{slug}/web?offset={off}&limit={PAGE}&sort=date")
        if not d:
            break
        batch = d["data"]["items"]
        items += batch
        off += len(batch)
        if off >= d["data"]["totalResults"] or not batch:
            break
    return items


async def card(c, slug):
    t = time.perf_counter()
    comp, crit, usr = await asyncio.gather(
        get(c, "composer", f"{B}/composer/metacritic/pages/games/{slug}/web"),
        reviews(c, "critic", slug),
        reviews(c, "user", slug),
    )
    return {"slug": slug, "sec": round(time.perf_counter() - t, 2), "critic": len(crit), "user": len(usr)}


async def main():
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=60, http2=False) as c:
        t0 = time.perf_counter()
        f = await get(c, "finder", SRC[src].format(n=N, off=OFF))
        slugs = [i["slug"] for i in f["data"]["items"]]
        t1 = time.perf_counter()
        cards = await asyncio.gather(*(card(c, s) for s in slugs))
        t2 = time.perf_counter()
    out = {
        "source": src, "concurrency": conc, "cards": len(cards),
        "finder_sec": round(t1 - t0, 2), "cards_wall_sec": round(t2 - t1, 2), "total_sec": round(t2 - t0, 2),
        "requests": stats["requests"], "mb": round(stats["bytes"] / 1e6, 2), "errors": stats["errors"],
        "critic_reviews": sum(x["critic"] for x in cards), "user_reviews": sum(x["user"] for x in cards),
        "latency": {k: {"n": len(v), "p50": round(statistics.median(v), 2), "max": round(max(v), 2)} for k, v in lat.items()},
        "slowest_cards": sorted(cards, key=lambda x: -x["sec"])[:3],
    }
    print(json.dumps(out, ensure_ascii=False))


asyncio.run(main())
