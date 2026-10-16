"""Daily pool of deep analyses on the free Gemini key, kept behind the service's own work.

The quota day is Google's: it resets at midnight Pacific. A new deep analysis is allowed only while all of these hold:
- the shared pool for the day is not used up (cached analyses do not count);
- this user has not used their own cap;
- after it, the day's token budget (a safe share of what the key is known to serve) still covers the service's
  reserve: review analyses and essences of new games always get their tokens first.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Analysis, DeepAnalysis, GameFacets

# Rows that never reached the model do not use the pool.
FREE_STATUSES = ("not_configured", "no_video", "search_error")
PENDING_STATUSES = ("queued", "running")


@dataclass
class DeepQuota:
    pool: int
    used: int
    user_used: int
    per_user: int
    service_tokens: int  # spent today by review analyses, essences and light essences
    deep_tokens: int  # spent today by deep analyses, pending ones counted at the estimate
    budget_tokens: int
    resets_at: datetime
    reason: str | None  # why a new analysis is refused right now; None means allowed

    @property
    def left(self) -> int:
        return 0 if self.reason else max(0, min(self.pool - self.used, self.per_user - self.user_used))

    def as_dict(self) -> dict:
        return {"pool": self.pool, "used": self.used, "user_used": self.user_used, "per_user": self.per_user,
                "left": self.left, "open": self.reason is None, "reason": self.reason, "resets_at": self.resets_at,
                "service_tokens": self.service_tokens, "deep_tokens": self.deep_tokens,
                "budget_tokens": self.budget_tokens}


def quota_day(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """Start of the current quota day and of the next one, both aware."""
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    return datetime.combine(local.date(), time(0), z), datetime.combine(local.date() + timedelta(days=1), time(0), z)


def refusal(s: Settings, *, used: int, user_used: int, service_tokens: int, deep_tokens: int) -> str | None:
    if used >= s.deep_daily_pool:
        return f"Дневной пул глубоких анализов исчерпан ({used} из {s.deep_daily_pool})"
    if user_used >= s.deep_per_user:
        return f"Ваш лимит на сегодня исчерпан ({user_used} из {s.deep_per_user})"
    budget = int(s.llm_daily_tokens * s.llm_safety)
    still_reserved = max(0, s.service_reserve_tokens - service_tokens)
    if service_tokens + deep_tokens + still_reserved + s.deep_est_tokens > budget:
        return "Сегодня квота модели нужна сервису для анализа отзывов"
    return None


async def deep_quota(session: AsyncSession, s: Settings, user: str | None, now: datetime) -> DeepQuota:
    start, resets_at = quota_day(now, s.quota_tz)
    today = DeepAnalysis.created_at >= start
    counted = DeepAnalysis.status.not_in(FREE_STATUSES)
    used = await session.scalar(select(func.count()).where(today, counted)) or 0
    user_used = (await session.scalar(select(func.count()).where(today, counted, DeepAnalysis.requested_by == user))
                 or 0) if user else 0
    service = (await session.scalar(select(func.coalesce(func.sum(Analysis.tokens_in + Analysis.tokens_out), 0))
                                    .where(Analysis.created_at >= start)) or 0)
    service += await session.scalar(select(func.coalesce(func.sum(GameFacets.tokens_in + GameFacets.tokens_out), 0))
                                    .where(GameFacets.essence_at >= start)) or 0
    spent = await session.scalar(select(func.coalesce(func.sum(DeepAnalysis.tokens_in + DeepAnalysis.tokens_out), 0))
                                 .where(today)) or 0
    pending = await session.scalar(select(func.count())
                                   .where(today, DeepAnalysis.status.in_(PENDING_STATUSES))) or 0
    deep = int(spent) + pending * s.deep_est_tokens
    return DeepQuota(pool=s.deep_daily_pool, used=used, user_used=user_used, per_user=s.deep_per_user,
                     service_tokens=int(service), deep_tokens=deep, budget_tokens=int(s.llm_daily_tokens * s.llm_safety),
                     resets_at=resets_at,
                     reason=refusal(s, used=used, user_used=user_used, service_tokens=int(service), deep_tokens=deep))
