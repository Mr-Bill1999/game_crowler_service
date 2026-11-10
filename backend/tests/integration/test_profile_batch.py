"""Rollout of game profiles: every new game, games due again, a quarter of the earlier catalogue per day."""

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.models import Game, GameProfile
from app.pipeline.ingest import wake_profile
from app.profile.pipeline import plan_batch

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)  # 13:00 in California: the quota day began 13 hours ago
S = Settings(profile_per_run=40, profile_new_days=3, profile_backlog_share=0.25)


def game(i: int, days_ago: float, **kw) -> Game:
    kw.setdefault("tier", "dormant")
    return Game(id=i, slug=f"g{i}", title=f"G{i}", discovered_at=NOW - timedelta(days=days_ago), **kw)


async def test_new_games_all_due_games_and_a_twelfth_of_the_daily_backlog_share(sessions):
    async with sessions() as s:
        s.add_all([game(i, 1) for i in range(1, 6)])  # 5 new
        s.add_all([game(i, 10) for i in range(100, 196)])  # 96 earlier, 97 with game 200 below
        s.add(game(200, 10))
        s.add(GameProfile(game_id=200, next_enrich_at=NOW - timedelta(hours=1), first_enriched_at=NOW - timedelta(days=2)))
        s.add(game(300, 1, archived_at=NOW))
        await s.commit()
        b = await plan_batch(s, S, NOW)
    assert (b.new, b.refresh, b.backlog) == (5, 1, 3)  # ceil(97 * 0.25) = 25 a day -> ceil(25 / 12) = 3 a run
    assert set(b.ids[:5]) == {1, 2, 3, 4, 5}  # new games come first
    assert 300 not in b.ids  # archived games are never enriched
    assert b.backlog_remaining == 96 - 3


async def test_backlog_stops_at_the_days_share(sessions):
    day_start = NOW - timedelta(hours=12)
    async with sessions() as s:
        s.add_all([game(i, 10) for i in range(1, 41)])  # 40 earlier games: 10 a day
        for i in range(1, 11):  # 10 of them already enriched today
            s.add(GameProfile(game_id=i, first_enriched_at=day_start + timedelta(minutes=i),
                              next_enrich_at=NOW + timedelta(days=1)))
        await s.commit()
        b = await plan_batch(s, S, NOW)
    assert b.backlog == 0 and b.backlog_left_today == 0 and b.backlog_remaining == 30


async def test_new_games_and_the_top_are_free_of_the_pace_and_the_top_goes_first(sessions):
    async with sessions() as s:
        s.add_all([game(1, 0.5), game(2, 30), game(3, 30, top_rank=4), game(4, 30)])
        await s.flush()
        due = NOW - timedelta(hours=1)
        s.add_all([GameProfile(game_id=2, next_enrich_at=due - timedelta(days=2), profile_evidence=0),
                   GameProfile(game_id=3, next_enrich_at=due, profile_evidence=0),
                   GameProfile(game_id=4, next_enrich_at=due - timedelta(days=1), profile_evidence=0)])
        await s.commit()
        b = await plan_batch(s, S, NOW, limit=40)
    assert b.ids[:4] == [1, 3, 2, 4]  # the new game, then the top one though it is due last, then by due date
    assert b.free == {1, 3}


async def test_due_games_go_by_tier_before_due_date(sessions):
    """Nov 2026: 3 300 quiet games due the day before kept hot games waiting up to 45 h."""
    async with sessions() as s:
        s.add_all([game(10, 30, tier="dormant"), game(11, 30, tier="cool"), game(12, 30, tier="warm"),
                   game(13, 30, tier="hot"), game(14, 30, tier="dormant", top_rank=2)])
        await s.flush()
        s.add_all([GameProfile(game_id=10, next_enrich_at=NOW - timedelta(days=2)),
                   GameProfile(game_id=11, next_enrich_at=NOW - timedelta(days=1)),
                   GameProfile(game_id=12, next_enrich_at=NOW - timedelta(hours=5)),
                   GameProfile(game_id=13, next_enrich_at=NOW - timedelta(minutes=5)),
                   GameProfile(game_id=14, next_enrich_at=NOW - timedelta(minutes=1))])
        await s.commit()
        b = await plan_batch(s, S, NOW, limit=40)
    assert b.ids == [14, 13, 12, 11, 10]  # the top, then hot, warm, cool, the quiet ones last


async def test_a_game_is_worth_reading_with_scores_the_top_or_five_steam_reviews(sessions):
    from app.models import Document
    from app.profile.pipeline import readable

    async with sessions() as s:
        s.add_all([game(10, 5), game(11, 5, critic_count=3), game(12, 5, top_rank=1), game(13, 5), game(14, 5)])
        await s.flush()
        for i in range(5):
            s.add(Document(game_id=13, source="steam_reviews", kind="user_review", ext_id=f"r{i}", text="t",
                           text_hash=f"h{i}"))
        for i in range(4):
            s.add(Document(game_id=14, source="steam_reviews", kind="user_review", ext_id=f"q{i}", text="t",
                           text_hash=f"g{i}"))
        await s.commit()
        got = {gid: await readable(s, await s.get(Game, gid), S) for gid in (10, 11, 12, 13, 14)}
    assert got == {10: False, 11: True, 12: True, 13: True, 14: False}


async def test_a_review_spike_brings_a_saturated_profile_forward(sessions):
    async with sessions() as s:
        s.add_all([game(1, 40), game(2, 40), game(3, 40)])
        await s.flush()
        s.add_all([GameProfile(game_id=1, fullness=90, next_enrich_at=NOW + timedelta(days=25)),
                   GameProfile(game_id=2, fullness=90, next_enrich_at=NOW - timedelta(hours=2)),
                   GameProfile(game_id=3, fullness=50, next_enrich_at=NOW + timedelta(hours=20))])
        await s.commit()
    async with sessions() as s:
        for i in (1, 2, 3):
            await wake_profile(s, i, NOW, timedelta(hours=24))
        await s.commit()
    async with sessions() as s:
        assert (await s.get(GameProfile, 1)).next_enrich_at == NOW
        assert (await s.get(GameProfile, 2)).next_enrich_at == NOW - timedelta(hours=2)  # already due
        assert (await s.get(GameProfile, 3)).next_enrich_at == NOW + timedelta(hours=20)  # due within a day anyway


async def test_each_kind_reports_what_its_latest_summary_covers(sessions):
    from app.models import Analysis
    from app.pipeline.ingest import covered_by_kind
    async with sessions() as s:
        s.add(game(1, 40))
        await s.flush()
        for kind, n, h in (("user", 202, 1), ("user", 1407, 3), ("critic", 103, 2), ("essence", 0, 3)):
            s.add(Analysis(game_id=1, kind=kind, texts_seen=n, provider="t", model="m", payload={},
                           created_at=NOW + timedelta(hours=h)))
        await s.commit()
    async with sessions() as s:
        assert await covered_by_kind(s, 1) == {"user": 1407, "critic": 103}
        assert await covered_by_kind(s, 2) == {}


async def test_a_spent_limit_holds_the_games_worth_reading_and_collects_the_rest(sessions, monkeypatch):
    """Nov 9-10, 2026: a spent limit stopped 12 runs whole. Now games worth reading wait, without taking a place,
    and the run collects the others."""
    from types import SimpleNamespace

    from app.pipeline import tasks

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, name, *args, **kw):
            self.events.append((name, kw))

    async with sessions() as s:
        s.add_all([game(i, 30, tier="hot", critic_count=5) for i in (1, 2, 3)])  # rated: worth reading
        s.add_all([game(i, 30) for i in range(10, 16)])  # quiet, nothing to read
        await s.flush()
        s.add_all([GameProfile(game_id=i, next_enrich_at=NOW - timedelta(hours=1)) for i in (1, 2, 3, *range(10, 16))])
        await s.commit()

    seen = []

    async def fake_enrich(ctx, game_id, read=True):
        seen.append((game_id, read))
        return {"built": False}

    spent = {"now": 1000}

    async def tokens(session, s, now):
        return spent["now"]

    monkeypatch.setattr(tasks, "enrich_game", fake_enrich)
    monkeypatch.setattr(tasks, "tokens_today", tokens)
    monkeypatch.setattr(tasks, "token_cap", lambda s: 1000)
    monkeypatch.setattr(tasks.clock, "now", lambda: NOW)
    bus = Bus()
    ctx = {"deps": SimpleNamespace(settings=S, sessions=sessions, bus=bus)}

    out = await tasks.enrich_due(ctx, limit=4)
    assert seen == [(10, False), (11, False), (12, False), (13, False)]  # four places, all collection
    assert out["held"] == {"cap": 3} and out["games"] == 4
    done = next(kw for name, kw in bus.events if name == "profile.done")
    assert done["held_by_cap"] == 3

    seen.clear()
    spent["now"] = 0
    await tasks.enrich_due(ctx, limit=4)
    assert seen[:3] == [(1, True), (2, True), (3, True)]  # budget back: hot games first, read
