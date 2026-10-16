"""Metacritic's "New Releases" block (top 20 on /game/): checked every hour, stored as the current rank on each game
and as one snapshot per day."""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Game, TopRelease
from app.scraper.normalize import FinderItem


def top_changes(before: list[str], after: list[str]) -> dict:
    return {"entered": [s for s in after if s not in before], "left": [s for s in before if s not in after]}


async def record_top(session: AsyncSession, items: list[FinderItem], now: datetime) -> dict:
    """Ranks follow the API order. Games that failed to ingest are left out rather than inventing rows."""
    before = list((await session.execute(
        select(Game.slug).where(Game.top_rank.is_not(None)).order_by(Game.top_rank))).scalars())
    ids = dict((await session.execute(select(Game.slug, Game.id).where(Game.slug.in_([i.slug for i in items])))).all())
    await session.execute(update(Game).where(Game.top_rank.is_not(None)).values(top_rank=None))
    await session.execute(delete(TopRelease).where(TopRelease.day == now.date()))
    for rank, item in enumerate(items, start=1):
        game_id = ids.get(item.slug)
        if game_id is None:
            continue
        await session.execute(update(Game).where(Game.id == game_id).values(top_rank=rank, top_seen_at=now))
        session.add(TopRelease(day=now.date(), rank=rank, game_id=game_id, metascore=item.metascore, observed_at=now))
    await session.commit()
    return {"size": len(items), "stored": sum(1 for i in items if i.slug in ids),
            **top_changes(before, [i.slug for i in items])}
