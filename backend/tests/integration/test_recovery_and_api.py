from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from app.models import DeepAnalysis, Game, PipelineRun
from app.pipeline.quota import expire_lost
from app.pipeline.tasks import close_lost_runs


async def add_game(sessions, gid: int = 1, slug: str = "some-game", **kw) -> None:
    async with sessions() as s:
        s.add(Game(id=gid, slug=slug, title=kw.pop("title", "Some Game"), genres=["Action"],
                   release_date=date(2026, 10, 14), **kw))
        await s.commit()


async def test_runs_left_by_a_killed_worker_are_closed_but_live_ones_are_not(sessions):
    now = datetime.now(UTC)
    async with sessions() as s:
        s.add_all([PipelineRun(id="old", kind="hourly", status="running", started_at=now - timedelta(hours=2), stats={}),
                   PipelineRun(id="busy", kind="hourly", status="running", started_at=now - timedelta(minutes=5),
                               stats={})])
        await s.commit()
        assert await close_lost_runs(s, now) == 1
        status = dict((await s.execute(text("select id, status from pipeline_runs"))).all())
    assert status == {"old": "interrupted", "busy": "running"}


async def test_lost_deep_analyses_give_their_slot_back(sessions):
    await add_game(sessions)
    async with sessions() as s:
        s.add(DeepAnalysis(game_id=1, status="running", requested_by="u"))
        s.add(DeepAnalysis(game_id=1, status="queued", requested_by="u"))
        await s.commit()
        await s.execute(text("update deep_analyses set updated_at = now() - interval '2 hours' where status='running'"))
        await s.commit()
        assert await expire_lost(s) == 1
        await s.commit()
        rows = sorted((await s.execute(text("select status from deep_analyses"))).scalars())
    assert rows == ["lost", "queued"]


async def test_deep_analyze_with_the_queue_down_answers_503_and_frees_the_slot(api, sessions):
    await add_game(sessions)
    api.queue.broken = True
    r = await api.post("/api/games/some-game/deep-analyze", headers={"X-Client-Id": "u1"})
    assert r.status_code == 503
    q = (await api.get("/api/deep/quota", headers={"X-Client-Id": "u1"})).json()
    assert q["used"] == 0 and q["user_used"] == 0


async def test_list_parameters_out_of_range_are_rejected_not_crashed(api, sessions):
    await add_game(sessions)
    for path in ["/api/games?limit=-1", "/api/games?offset=-5", "/api/games?limit=100000", "/api/games?sort=title",
                 "/api/pipeline/runs?limit=-1", "/api/pipeline/runs?limit=100000",
                 "/api/games/some-game/reviews?limit=-1", "/api/games/some-game/reviews?kind=both",
                 "/api/top?day=yesterday"]:
        r = await api.get(path)
        assert r.status_code == 422, (path, r.status_code, r.text[:200])


async def test_search_text_is_text_not_a_pattern(api, sessions):
    await add_game(sessions, 1, "a", title="Alpha")
    await add_game(sessions, 2, "b", title="100% Orange Juice")
    for q, expected in [("%", ["b"]), ("_", []), ("\\", []), ("'; drop table games; --", []), ("x" * 5000, [])]:
        r = await api.get("/api/games", params={"q": q})
        assert r.status_code == 200, (q[:20], r.status_code, r.text[:200])
        assert [g["slug"] for g in r.json()["items"]] == expected, q[:20]


async def test_unknown_game_is_404(api, sessions):
    for path in ["/api/games/nope", "/api/games/nope/reviews", "/api/games/..%2F..%2Fetc/reviews"]:
        assert (await api.get(path)).status_code == 404, path
    assert (await api.post("/api/games/nope/deep-analyze")).status_code == 404


async def test_made_up_client_ids_do_not_multiply_the_limit(api, sessions):
    for i in range(1, 6):
        await add_game(sessions, i, f"g{i}", title=f"Game {i}")
    codes = [(await api.post(f"/api/games/g{i}/deep-analyze", headers={"X-Client-Id": f"forged-{i}"})).status_code
             for i in range(1, 6)]
    assert codes == [202, 202, 202, 429, 429]  # three per address, whatever the client id says
    r = await api.post("/api/games/g5/deep-analyze", headers={"X-Client-Id": "someone", "X-Real-IP": "203.0.113.9"})
    assert r.status_code == 202  # another address (as nginx reports it) has its own allowance


async def test_a_restarted_worker_closes_its_own_runs_at_once_but_not_cli_ones(sessions):
    now = datetime.now(UTC)
    async with sessions() as s:
        s.add_all([PipelineRun(id="w", kind="hourly", status="running", started_at=now - timedelta(minutes=3),
                               stats={}, owner="worker"),
                   PipelineRun(id="c", kind="hourly", status="running", started_at=now - timedelta(minutes=3),
                               stats={}, owner="cli")])
        await s.commit()
        assert await close_lost_runs(s, now, worker_restarted=True) == 1
        status = dict((await s.execute(text("select id, status from pipeline_runs"))).all())
    assert status == {"w": "interrupted", "c": "running"}


async def test_event_history_must_not_be_negative(api, sessions):
    assert (await api.get("/api/events/stream?history=-1")).status_code == 422
