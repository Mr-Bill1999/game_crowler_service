"""Quarantine for things that fail every time: a card Metacritic answers without its product, an essence the model
answers empty. Before it, four such cards failed every hour for days (~100 errors a day that meant nothing and hid
the real ones). After FAILS_BEFORE_PAUSE failures in a row a game waits, twice as long after each next failure, up
to a week; a card that keeps coming back empty is archived. One success clears the count."""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Game
from app.scraper.normalize import EmptyCard

FAILS_BEFORE_PAUSE = 3
MAX_PAUSE_H = 7 * 24


def pause_for(fails: int) -> timedelta | None:
    """None: retry on the next run as usual; else how long to wait after this many failures in a row."""
    if fails < FAILS_BEFORE_PAUSE:
        return None
    return timedelta(hours=min(2 ** (fails - FAILS_BEFORE_PAUSE), MAX_PAUSE_H))


async def card_failed(session: AsyncSession, slug: str, error: Exception, now: datetime) -> dict | None:
    """Count one failed refresh of a stored game; returns what was decided, or None for a game not stored yet
    (discovery offers it again on its own)."""
    game = await session.scalar(select(Game).where(Game.slug == slug))
    if game is None:
        return None
    game.fail_count = (game.fail_count or 0) + 1
    game.last_error = f"{type(error).__name__}: {error}"[:300]
    out = {"slug": slug, "fails": game.fail_count}
    if isinstance(error, EmptyCard) and game.fail_count >= FAILS_BEFORE_PAUSE:
        game.archived_at, game.next_refresh_at = now, None
        out["archived"] = True
    elif pause := pause_for(game.fail_count):
        game.next_refresh_at = now + pause
        out["paused_h"] = round(pause.total_seconds() / 3600)
    await session.commit()
    return out


async def cards_ok(session: AsyncSession, slugs: list[str]) -> None:
    if slugs:
        await session.execute(update(Game).where(Game.slug.in_(slugs), Game.fail_count > 0)
                              .values(fail_count=0, last_error=None))
        await session.commit()


async def essence_failed(session: AsyncSession, game_id: int, now: datetime) -> dict:
    game = await session.get(Game, game_id)
    game.facet_fail_count = (game.facet_fail_count or 0) + 1
    pause = pause_for(game.facet_fail_count)
    game.facet_retry_at = now + pause if pause else None
    await session.commit()
    return {"fails": game.facet_fail_count, "paused_h": round(pause.total_seconds() / 3600) if pause else None}


async def essence_ok(session: AsyncSession, game_id: int) -> None:
    await session.execute(update(Game).where(Game.id == game_id, Game.facet_fail_count > 0)
                          .values(facet_fail_count=0, facet_retry_at=None))
