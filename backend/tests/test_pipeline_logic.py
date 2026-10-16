import asyncio
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.config import Settings
from app.pipeline.discovery import fresh_items
from app.pipeline.tiers import TierInput, compute_tier, is_spike, needs_analysis
from app.scraper.client import RateLimiter
from app.scraper.normalize import FinderItem

S = Settings()
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def tier(age_days: int, reviews: int = 50, quiet_days: int | None = 0, hot_until=None) -> str:
    growth = None if quiet_days is None else NOW - timedelta(days=quiet_days)
    t = TierInput(NOW.date() - timedelta(days=age_days), NOW, reviews, growth, hot_until)
    return compute_tier(t, NOW, S)[0]


@pytest.mark.parametrize("age,expected", [(0, "hot"), (14, "hot"), (15, "warm"), (45, "warm"), (46, "cool"),
                                          (90, "cool")])
def test_tier_by_age(age, expected):
    assert tier(age) == expected


def test_old_game_archives_only_when_quiet():
    assert tier(120, quiet_days=1) == "cool"
    assert tier(120, quiet_days=7) == "archived"
    assert tier(120, quiet_days=None) == "archived"


def test_games_without_reviews_go_dormant_then_archive():
    assert tier(10, reviews=0) == "dormant"
    assert tier(31, reviews=0) == "archived"


def test_spike_returns_game_to_hot():
    assert tier(60, hot_until=NOW + timedelta(hours=3)) == "hot"
    assert tier(60, hot_until=NOW - timedelta(hours=1)) == "cool"


def test_next_refresh_matches_tier():
    t = TierInput(NOW.date(), NOW, 10, NOW, None)
    _, nxt = compute_tier(t, NOW, S)
    assert timedelta(minutes=50) <= nxt - NOW <= timedelta(hours=1)
    archived = TierInput(NOW.date() - timedelta(days=200), NOW, 10, None, None)
    assert compute_tier(archived, NOW, S) == ("archived", None)


def test_spike_needs_both_ratio_and_minimum():
    assert is_spike(100, 110, S)
    assert not is_spike(100, 109, S)
    assert not is_spike(10, 13, S)  # +30% but only 3 reviews


def test_analysis_trigger_counts_texts_and_runs_once_a_day():
    kw = {"now": NOW, "s": S}
    assert not needs_analysis(critic_texts=2, user_texts=4, analyzed_texts=0, analyzed_at=None, **kw)
    assert needs_analysis(critic_texts=3, user_texts=0, analyzed_texts=0, analyzed_at=None, **kw)
    assert needs_analysis(critic_texts=0, user_texts=5, analyzed_texts=0, analyzed_at=None, **kw)
    earlier_today = NOW.replace(hour=0, minute=30)
    assert not needs_analysis(critic_texts=50, user_texts=500, analyzed_texts=10, analyzed_at=earlier_today, **kw)
    yesterday = NOW - timedelta(days=1)
    assert needs_analysis(critic_texts=50, user_texts=75, analyzed_texts=100, analyzed_at=yesterday, **kw)  # +25
    assert needs_analysis(critic_texts=10, user_texts=8, analyzed_texts=15, analyzed_at=yesterday, **kw)  # +20%
    assert not needs_analysis(critic_texts=50, user_texts=55, analyzed_texts=100, analyzed_at=yesterday, **kw)
    assert not needs_analysis(critic_texts=4, user_texts=2, analyzed_texts=5, analyzed_at=yesterday, **kw)  # floor


def item(i: int) -> FinderItem:
    return FinderItem(i, f"g{i}", f"Game {i}", date(2026, 10, 14), None, 0, [])


def test_fresh_items_skip_known_and_duplicates_across_lists():
    seen: set[int] = set()
    assert [x.id for x in fresh_items([item(1), item(2), item(3)], {2}, seen)] == [1, 3]
    assert [x.id for x in fresh_items([item(3), item(4)], set(), seen)] == [4]


async def test_rate_limiter_spaces_requests():
    lim = RateLimiter(rps=20)
    t0 = time.monotonic()
    await asyncio.gather(*(lim.acquire() for _ in range(6)))
    assert time.monotonic() - t0 >= 0.24  # 5 gaps of 50 ms
