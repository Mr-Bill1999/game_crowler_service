"""Who is on the site right now. Each open tab pings every 30 s with the anonymous id the browser already keeps
(`ga:client-id`) and the page it shows; nothing else about the visitor is stored. A visitor counts as online
while their last ping is under ONLINE_S old. Daily uniques are HyperLogLogs, a few KB each whatever the traffic,
kept for HISTORY_DAYS."""

import re
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

ONLINE_S = 75  # two missed pings and the visitor is gone
HISTORY_DAYS = 30
TZ = ZoneInfo("Europe/Moscow")

SEEN = "presence:seen"  # zset: client id -> last ping, unix seconds
PAGE = "presence:page"  # hash: client id -> what that tab shows
DAY = "presence:day:"  # + YYYY-MM-DD, HyperLogLog of client ids

ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def page_of(path: str, play: bool = False) -> str:
    """Folds a path into a page kind, so the panel lists "карточка игры" once instead of every slug."""
    if play:
        return "game"
    path = (path or "/").split("?")[0].split("#")[0]
    if path == "/":
        return "welcome"
    if path.startswith("/game/"):
        return "card"
    if path == "/games":
        return "catalog"
    if path.startswith("/devpanel"):
        return "devpanel"
    return "other"


def day_key(ts: float) -> str:
    return DAY + datetime.fromtimestamp(ts, UTC).astimezone(TZ).date().isoformat()


async def ping(redis: Redis, client: str, page: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    key = day_key(now)
    async with redis.pipeline(transaction=False) as p:
        p.zadd(SEEN, {client: now})
        p.hset(PAGE, client, page)
        p.pfadd(key, client)
        p.expire(key, (HISTORY_DAYS + 1) * 86400)
        await p.execute()


async def leave(redis: Redis, client: str) -> None:
    async with redis.pipeline(transaction=False) as p:
        p.zrem(SEEN, client)
        p.hdel(PAGE, client)
        await p.execute()


async def snapshot(redis: Redis, days: int = 14, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    gone = await redis.zrangebyscore(SEEN, 0, now - ONLINE_S)
    if gone:
        async with redis.pipeline(transaction=False) as p:
            p.zrem(SEEN, *gone)
            p.hdel(PAGE, *gone)
            await p.execute()
    online = await redis.zrange(SEEN, 0, -1)
    pages: dict[str, int] = {}
    if online:
        for page in await redis.hmget(PAGE, online):
            pages[page or "other"] = pages.get(page or "other", 0) + 1

    today = datetime.fromtimestamp(now, UTC).astimezone(TZ).date()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    async with redis.pipeline(transaction=False) as p:
        for d in dates:
            p.pfcount(DAY + d.isoformat())
        counts = await p.execute()
    async with redis.pipeline(transaction=False) as p:
        p.pfcount(*[DAY + d.isoformat() for d in dates[-7:]])
        p.pfcount(*[DAY + d.isoformat() for d in dates])
        week, period = await p.execute()

    return {
        "online": len(online),
        "pages": pages,
        "today": counts[-1],
        "week": week,
        "period": period,
        "days": [{"date": d.isoformat(), "visitors": c} for d, c in zip(dates, counts, strict=True)],
        "online_window_s": ONLINE_S,
    }
