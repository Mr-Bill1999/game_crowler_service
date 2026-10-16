"""Refresh tiers: how often a game is polled, decided from release age and review activity. Pure logic."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.core.config import Settings

INTERVALS = {"hot": timedelta(hours=1), "warm": timedelta(hours=6), "cool": timedelta(days=1),
             "dormant": timedelta(days=1)}
# Schedule slightly early so the hourly cron picks the game up on the matching tick.
SLACK = timedelta(minutes=5)


@dataclass
class TierInput:
    release_date: date | None
    discovered_at: datetime
    total_reviews: int
    last_growth_at: datetime | None
    hot_until: datetime | None


def age_days(t: TierInput, now: datetime) -> int:
    start = t.release_date or t.discovered_at.date()
    return (now.date() - start).days


def compute_tier(t: TierInput, now: datetime, s: Settings) -> tuple[str, datetime | None]:
    """Returns (tier, next_refresh_at); archived games get no next refresh."""
    age = age_days(t, now)
    if t.total_reviews == 0:
        tier = "archived" if age > s.dormant_days else "dormant"
    elif t.hot_until and now < t.hot_until or age <= s.hot_days:
        tier = "hot"
    elif age <= s.warm_days:
        tier = "warm"
    elif age <= s.cool_days:
        tier = "cool"
    else:
        quiet = t.last_growth_at is None or now - t.last_growth_at >= timedelta(days=s.quiet_days)
        tier = "archived" if quiet else "cool"
    if tier == "archived":
        return tier, None
    return tier, now + INTERVALS[tier] - SLACK


def is_spike(snapshot_count: int, total_now: int, s: Settings) -> bool:
    new = total_now - snapshot_count
    return new >= s.spike_min_new and new >= s.spike_ratio * max(snapshot_count, 1)


def needs_analysis(*, critic_texts: int, user_texts: int, analyzed_texts: int,
                   analyzed_at: datetime | None, now: datetime, s: Settings) -> bool:
    """Counts text reviews only (critics + player reviews long enough to be read), never bare ratings:
    the model only sees texts, so ratings alone must not trigger a run. At most once per calendar day."""
    if critic_texts < s.analysis_min_critic and user_texts < s.analysis_min_user:
        return False
    if analyzed_at is None:
        return True
    if analyzed_at.date() >= now.date():
        return False
    new = critic_texts + user_texts - analyzed_texts
    return new >= s.reanalyze_floor and (new >= s.reanalyze_min_new or new >= s.reanalyze_ratio * max(analyzed_texts, 1))
