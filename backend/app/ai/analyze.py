"""Three analyses per game: critic summary, player summary, essence. Large review sets go through map-reduce."""

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import ESSENCE_SCHEMA, REVIEW_SUMMARY_SCHEMA, LLMProvider, LLMResult
from app.ai.prompts import (
    CHUNK_SYSTEM,
    ESSENCE_SYSTEM,
    REDUCE_SYSTEM,
    SYSTEM,
    chunk_reviews,
    essence_prompt,
    reduce_prompt,
    summary_prompt,
)
from app.models import Game, Review

MIN_REVIEWS = {"critic": 3, "user": 5}


@dataclass
class TaskOutcome:
    kind: str
    data: dict | None = None
    calls: list[LLMResult] = field(default_factory=list)
    reviews_seen: int = 0
    error: str | None = None

    @property
    def tokens_in(self) -> int:
        return sum(c.tokens_in for c in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(c.tokens_out for c in self.calls)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def latency_s(self) -> float:
        return sum(c.latency_s for c in self.calls)


async def load_reviews(session: AsyncSession, game_id: int, kind: str) -> list[Review]:
    rows = await session.execute(select(Review).where(Review.game_id == game_id, Review.kind == kind)
                                 .order_by(Review.date.desc().nulls_last(), Review.id))
    return list(rows.scalars())


async def summarize(provider: LLMProvider, g: Game, kind: str, reviews: list[Review], model: str,
                    thinking_level: str | None) -> TaskOutcome:
    out = TaskOutcome(kind, reviews_seen=len(reviews))
    if len(reviews) < MIN_REVIEWS[kind]:
        return out
    kw = {"schema": REVIEW_SUMMARY_SCHEMA, "model": model, "thinking_level": thinking_level}
    try:
        chunks = chunk_reviews(reviews)
        if len(chunks) == 1:
            r = await provider.generate_json(system=SYSTEM, prompt=summary_prompt(g, kind, chunks[0]), **kw)
            out.calls.append(r)
            out.data = r.data
            return out
        parts = await asyncio.gather(*(
            provider.generate_json(system=CHUNK_SYSTEM, prompt=summary_prompt(g, kind, c, (i + 1, len(chunks))), **kw)
            for i, c in enumerate(chunks)))
        out.calls += parts
        r = await provider.generate_json(system=REDUCE_SYSTEM, prompt=reduce_prompt(g, kind, [p.data for p in parts]),
                                         **kw)
        out.calls.append(r)
        out.data = r.data
    except Exception as e:  # noqa: BLE001  one failed task must not lose the others
        out.error = f"{type(e).__name__}: {e}"[:500]
    return out


async def essence(provider: LLMProvider, g: Game, critic: dict | None, user: dict | None, model: str,
                  thinking_level: str | None) -> TaskOutcome:
    out = TaskOutcome("essence")
    try:
        r = await provider.generate_json(system=ESSENCE_SYSTEM, prompt=essence_prompt(g, critic, user),
                                         schema=ESSENCE_SCHEMA, model=model, thinking_level=thinking_level)
        out.calls.append(r)
        out.data = r.data
    except Exception as e:  # noqa: BLE001  one failed task must not lose the others
        out.error = f"{type(e).__name__}: {e}"[:500]
    return out


async def analyze_game(provider: LLMProvider, g: Game, critic_reviews: list[Review], user_reviews: list[Review],
                       model: str, thinking_level: str | None = None) -> dict[str, TaskOutcome]:
    critic, user = await asyncio.gather(
        summarize(provider, g, "critic", critic_reviews, model, thinking_level),
        summarize(provider, g, "user", user_reviews, model, thinking_level),
    )
    ess = await essence(provider, g, critic.data, user.data, model, thinking_level)
    return {"critic": critic, "user": user, "essence": ess}
