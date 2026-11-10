"""Profile budget: who is worth the model's tokens, and the even pace the backfill keeps (November 2026: a backfill
spent the whole day's budget in an hour and a half, and new games waited until the next quota day)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.models import Game
from app.profile.pipeline import admit, next_pass, pace_limit, saturated, worth_reading

CAP = 2_400_000
DAY = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)  # quota day starts at midnight in California


@pytest.mark.parametrize(("rated", "top", "reviews", "want"), [
    (True, False, 0, True),     # Metacritic scores
    (False, True, 0, True),     # in the top
    (False, False, 5, True),    # enough Steam reviews
    (False, False, 4, False),   # a few Steam reviews: not yet
    (False, False, 0, False),   # nothing to read: the 3 887 that never reached a profile
])
def test_worth_reading(rated, top, reviews, want):
    assert worth_reading(rated, top, reviews, min_reviews=5) is want


def at(hours: float) -> datetime:
    return DAY + timedelta(hours=hours)


def test_pace_grows_by_a_24th_an_hour_and_carries_quiet_hours_over():
    assert pace_limit(CAP, at(0.2), DAY, 0.25) == CAP // 24
    assert pace_limit(CAP, at(5.9), DAY, 0.25) == CAP * 6 // 24
    assert pace_limit(CAP, at(15), DAY, 0.25) == CAP * 16 // 24


def test_pace_keeps_the_reserve_for_new_games_until_the_last_two_hours():
    assert pace_limit(CAP, at(18), DAY, 0.25) == int(CAP * 0.75)
    assert pace_limit(CAP, at(21.5), DAY, 0.25) == int(CAP * 0.75)
    assert pace_limit(CAP, at(22), DAY, 0.25) == CAP * 23 // 24
    assert pace_limit(CAP, at(23.5), DAY, 0.25) == CAP


def test_pace_before_the_day_start_is_the_first_hour():
    assert pace_limit(CAP, DAY - timedelta(minutes=5), DAY, 0.25) == CAP // 24


S = Settings()


@pytest.mark.parametrize(("fullness", "built", "want"), [(80, True, True), (100, True, True), (79, True, False),
                                                         (95, False, False)])
def test_a_built_profile_at_80_is_saturated(fullness, built, want):
    assert saturated(fullness, built, S) is want


def test_a_saturated_profile_waits_30_days_but_never_while_the_game_is_hot():
    now = at(5)
    warm = Game(id=1, slug="w", title="W", tier="warm")
    assert next_pass(warm, now, S) == now + timedelta(hours=72)
    assert next_pass(warm, now, S, full=True) == now + timedelta(days=30)
    young = Game(id=2, slug="y", title="Y", tier="hot")
    assert next_pass(young, now, S, full=True) == now + timedelta(hours=24)  # first two weeks: reception still moves
    spiked = Game(id=3, slug="s", title="S", tier="warm", hot_until=now + timedelta(hours=10))
    assert next_pass(spiked, now, S, full=True) == now + timedelta(hours=72)  # a spike keeps the usual cadence
    cooled = Game(id=4, slug="c", title="C", tier="cool", hot_until=now - timedelta(hours=1))
    assert next_pass(cooled, now, S, full=True) == now + timedelta(days=30)
    gone = Game(id=5, slug="g", title="G", tier="warm", archived_at=now)
    assert next_pass(gone, now, S, full=True) is None


def test_quiet_games_with_nothing_to_read_come_back_in_a_fortnight():
    now = at(5)
    quiet = Game(id=6, slug="q", title="Q", tier="dormant")
    assert next_pass(quiet, now, S, waiting=True) == now + timedelta(days=14)
    assert next_pass(quiet, now, S, waiting=False) == now + timedelta(days=7)  # five Steam reviews: worth a weekly look
    assert next_pass(Game(id=7, slug="c", title="C", tier="cool"), now, S, waiting=True) == now + timedelta(days=7)


@pytest.mark.parametrize(("will_read", "free", "spent", "want"), [
    (True, False, 100, "read"),
    (True, False, 600, "pace"),       # over the pace: held, its place goes to the next game
    (True, True, 600, "read"),        # new games and the top are free of the pace
    (False, False, 600, "read"),      # nothing to read: the pace is about tokens, this pass costs none
    (True, True, 1000, "cap"),        # the limit is spent: held
    (False, False, 1000, "collect"),  # ...but collection goes on (Nov 9-10: 12 runs stopped whole)
])
def test_the_token_limit_holds_reading_never_collection(will_read, free, spent, want):
    assert admit(will_read, free, spent, cap=1000, pace=500) == want
