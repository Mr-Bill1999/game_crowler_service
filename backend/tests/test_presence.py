import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.api import presence
from app.api.main import app

T = 1_800_000_000.0  # some fixed moment


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def test_online_counts_only_recent_pings_and_groups_pages(redis):
    await presence.ping(redis, "aaaaaaaa-1", "card", now=T - presence.ONLINE_S - 5)  # went quiet
    await presence.ping(redis, "bbbbbbbb-2", "card", now=T - 10)
    await presence.ping(redis, "cccccccc-3", "game", now=T - 1)
    snap = await presence.snapshot(redis, days=3, now=T)
    assert snap["online"] == 2
    assert snap["pages"] == {"card": 1, "game": 1}
    assert snap["today"] == 3  # the quiet one still visited today
    assert [d["visitors"] for d in snap["days"]] == [0, 0, 3]
    assert await redis.hget(presence.PAGE, "aaaaaaaa-1") is None  # trimmed with the online set


async def test_a_visitor_pinging_many_times_is_one_visitor(redis):
    for i in range(20):
        await presence.ping(redis, "dddddddd-4", "catalog", now=T + i)
    snap = await presence.snapshot(redis, now=T + 20)
    assert (snap["online"], snap["today"], snap["week"]) == (1, 1, 1)


async def test_leaving_drops_the_visitor_at_once(redis):
    await presence.ping(redis, "eeeeeeee-5", "welcome", now=T)
    await presence.leave(redis, "eeeeeeee-5")
    snap = await presence.snapshot(redis, now=T + 1)
    assert snap["online"] == 0 and snap["today"] == 1


def test_paths_fold_into_page_kinds():
    assert presence.page_of("/game/elden-ring#reviews") == "card"
    assert presence.page_of("/games?top=1") == "catalog"
    assert presence.page_of("/") == "welcome"
    assert presence.page_of("/", play=True) == "game"
    assert presence.page_of("/wp-admin") == "other"


async def test_api_accepts_beacons_and_rejects_junk_ids(redis):
    app.state.redis = redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ok = await c.post("/api/presence", json={"id": "ffffffff-6", "path": "/games"})
        beacon = await c.post("/api/presence", content='{"id":"gggggggg-7","path":"/","play":true}',
                              headers={"Content-Type": "text/plain"})
        junk = await c.post("/api/presence", json={"id": "<script>", "path": "/"})
        snap = (await c.get("/api/presence")).json()
        await c.post("/api/presence/leave", content='{"id":"ffffffff-6"}')
        after = (await c.get("/api/presence")).json()
    assert (ok.status_code, beacon.status_code, junk.status_code) == (204, 204, 422)
    assert snap["online"] == 2 and snap["pages"] == {"catalog": 1, "game": 1}
    assert after["online"] == 1
