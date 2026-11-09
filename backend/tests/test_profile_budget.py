"""Profile budget: who is worth the model's tokens, and the even pace the backfill keeps (November 2026: a backfill
spent the whole day's budget in an hour and a half, and new games waited until the next quota day)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.profile.pipeline import pace_limit, worth_reading

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
