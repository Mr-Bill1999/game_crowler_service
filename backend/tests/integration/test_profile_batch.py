"""Rollout of game profiles: every new game, games due again, a quarter of the earlier catalogue per day."""

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.models import Game, GameProfile
from app.profile.pipeline import plan_batch

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)  # 13:00 in California: the quota day began 13 hours ago
S = Settings(profile_per_run=40, profile_new_days=3, profile_backlog_share=0.25)


def game(i: int, days_ago: float, **kw) -> Game:
    return Game(id=i, slug=f"g{i}", title=f"G{i}", discovered_at=NOW - timedelta(days=days_ago), tier="dormant", **kw)


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
