"""Keeping light essences and facet vectors current, within the free tier.

A game gets a light essence once it has a store description long enough to say how it plays, and again only when
its title, genres or description change. Vectors follow the essence; they are spent from a daily allowance
(1000 texts per embedding model on the free tier) and whatever does not fit waits for the next quota day.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gemini import GeminiProvider
from app.ai.light_essence import (
    LIGHT_ESSENCE_SCHEMA,
    LIGHT_ESSENCE_SYSTEM,
    MIN_DESCRIPTION,
    clean_tags,
    essence_prompt,
    flat_tags,
    source_hash,
    texts,
)
from app.core.config import Settings
from app.events.bus import EventBus
from app.models import FACET_DIM, Game, GameFacets
from app.pipeline.quota import quota_day

FACETS = ("gameplay", "world")
PER_MINUTE_PAUSE_S = 62  # embeddings: 100 texts a minute per model


async def stale_games(session: AsyncSession, limit: int) -> list[Game]:
    """Games with a usable description and no essence, or one built from an older description; newest first."""
    rows = (await session.execute(
        select(Game, GameFacets.source_hash).outerjoin(GameFacets, GameFacets.game_id == Game.id)
        .where(func.length(func.coalesce(Game.description, "")) >= MIN_DESCRIPTION)
        .order_by(Game.release_date.desc().nulls_last(), Game.id))).all()
    return [g for g, h in rows if h != source_hash(g)][:limit]


async def build_essences(provider: GeminiProvider, games: list[Game], s: Settings, sessions, bus: EventBus) -> dict:
    stats = {"ok": 0, "error": 0, "tokens_in": 0, "tokens_out": 0}

    async def one(g: Game) -> None:
        try:
            r = await provider.generate_json(system=LIGHT_ESSENCE_SYSTEM, prompt=essence_prompt(g),
                                             schema=LIGHT_ESSENCE_SCHEMA, model=s.llm_model,
                                             thinking_level=s.llm_thinking_level, max_output_tokens=2048)
        except Exception as e:  # noqa: BLE001 - one game failing must not stop the others
            stats["error"] += 1
            await bus.emit("facets.essence", "error", slug=g.slug, error=f"{type(e).__name__}: {e}"[:300])
            return
        e = clean_tags(r.data)
        async with sessions() as session:
            row = await session.get(GameFacets, g.id) or GameFacets(game_id=g.id)
            row.essence, row.tags, row.series = e, flat_tags(e), (e.get("series") or "").strip()[:200] or None
            row.confidence, row.source_hash, row.model = e.get("confidence"), source_hash(g), r.model
            row.tokens_in, row.tokens_out, row.essence_at = r.tokens_in, r.tokens_out, datetime.now(UTC)
            session.add(row)
            await session.commit()
        stats["ok"] += 1
        stats["tokens_in"] += r.tokens_in
        stats["tokens_out"] += r.tokens_out
        await bus.emit("facets.essence", slug=g.slug, tags=len(row.tags), confidence=row.confidence,
                       tokens_in=r.tokens_in, tokens_out=r.tokens_out)

    await asyncio.gather(*(one(g) for g in games))
    return stats


async def embed_pending(provider: GeminiProvider, s: Settings, sessions, bus: EventBus, now: datetime) -> dict:
    """Vectors for essences that have none or have changed, as many as today's allowance allows."""
    start, _ = quota_day(now, s.quota_tz)
    async with sessions() as session:
        used = (await session.scalar(select(func.count()).where(GameFacets.embedded_at >= start)) or 0) * len(FACETS)
        pending = (await session.execute(
            select(GameFacets, Game.title).join(Game, Game.id == GameFacets.game_id)
            .where((GameFacets.embed_hash.is_(None)) | (GameFacets.embed_hash != GameFacets.source_hash))
            .order_by(GameFacets.essence_at))).all()
    room = max(0, s.embed_daily_texts - used) // len(FACETS)
    todo, waiting = pending[:room], len(pending) - min(room, len(pending))
    per_batch = max(1, s.embed_batch // len(FACETS))
    done = 0
    for i in range(0, len(todo), per_batch):
        if i:
            await asyncio.sleep(PER_MINUTE_PAUSE_S)
        batch = todo[i:i + per_batch]
        items = [texts(f.essence, title)[facet] for f, title in batch for facet in FACETS]
        try:
            vecs = await provider.embed(items, model=s.embed_model, dim=FACET_DIM)
        except Exception as e:  # noqa: BLE001 - a daily limit or outage: keep what is done, retry next run
            await bus.emit("facets.embed", "error", done=done, error=f"{type(e).__name__}: {e}"[:300])
            waiting += len(todo) - i
            break
        stamp = datetime.now(UTC)
        async with sessions() as session:
            for k, (f, _) in enumerate(batch):
                row = await session.get(GameFacets, f.game_id)
                if row is None or row.source_hash != f.source_hash:
                    continue  # the essence changed meanwhile: its own vectors come next run
                row.gameplay_vec, row.world_vec = vecs[2 * k], vecs[2 * k + 1]
                row.embed_model, row.embed_hash, row.embedded_at = s.embed_model, f.source_hash, stamp
            await session.commit()
        done += len(batch)
        await bus.emit("facets.embed", games=len(batch), texts=len(items), model=s.embed_model)
    return {"embedded": done, "waiting": waiting, "texts_used_today": used + done * len(FACETS)}
