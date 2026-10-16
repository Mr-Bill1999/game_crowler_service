from datetime import UTC, datetime

from app.core.config import Settings
from app.pipeline.quota import quota_day, refusal

S = Settings(deep_daily_pool=5, deep_per_user=2, deep_est_tokens=36_000, llm_daily_tokens=1_160_000,
             llm_safety=0.8, service_reserve_tokens=190_000)


def test_quiet_day_allows_a_deep_analysis():
    assert refusal(S, used=0, user_used=0, service_tokens=20_000, deep_tokens=0) is None


def test_pool_and_user_caps():
    assert "пул" in refusal(S, used=5, user_used=0, service_tokens=0, deep_tokens=0)
    assert "Ваш лимит" in refusal(S, used=2, user_used=2, service_tokens=0, deep_tokens=0)


def test_service_reserve_comes_first():
    # budget 928k: a heavy review day plus the rest of the reserve leaves no room for 36k more
    assert refusal(S, used=1, user_used=0, service_tokens=880_000, deep_tokens=36_000) is not None
    # the reserve is held even before the service has spent it: 4 deep analyses already eat 144k of 928k
    assert refusal(S, used=4, user_used=0, service_tokens=0, deep_tokens=144_000) is None
    assert refusal(S, used=1, user_used=0, service_tokens=0, deep_tokens=720_000) is not None


def test_quota_day_is_pacific():
    # 06:30 UTC on 16.10 is still 15.10 in California (PDT, UTC-7)
    start, nxt = quota_day(datetime(2026, 10, 16, 6, 30, tzinfo=UTC), "America/Los_Angeles")
    assert start.astimezone(UTC) == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert nxt.astimezone(UTC) == datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
