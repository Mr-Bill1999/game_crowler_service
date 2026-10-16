"""Fetch one game card plus its review delta and upsert everything. One call = one game."""

import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.selection import SUBSTANTIVE_WORDS
from app.core import clock
from app.core.config import Settings
from app.events.bus import EventBus
from app.models import Game, GamePlatform, RawResponse, Review
from app.pipeline.tiers import TierInput, compute_tier, is_spike, needs_analysis
from app.scraper.metacritic import Metacritic
from app.scraper.normalize import GameCard, ReviewData, parse_composer, parse_review

CARD_FIELDS = ("slug", "title", "description", "release_date", "genres", "developer", "publisher", "rating",
               "must_play", "cover_url", "video_title", "video_embed_url", "video_manifest_url",
               "metascore", "critic_count", "userscore", "user_count")


@dataclass
class Deps:
    settings: Settings
    mc: Metacritic
    bus: EventBus
    sessions: async_sessionmaker[AsyncSession]


@dataclass
class IngestResult:
    game_id: int
    slug: str
    is_new: bool
    changed: bool
    new_reviews: int
    tier: str
    analyze: bool


def _fingerprint(card: GameCard) -> tuple:
    return (card.metascore, card.userscore, card.critic_count, card.user_count,
            tuple(sorted((p.slug, p.critic_count) for p in card.platforms)))


def _stored_fingerprint(g: Game) -> tuple:
    return (g.metascore, g.userscore, g.critic_count, g.user_count,
            tuple(sorted((p.slug, p.critic_count) for p in g.platforms)))


def _sync_platforms(game: Game, card: GameCard) -> None:
    """Update in place: replacing the collection would insert before delete and hit the primary key."""
    existing = {p.slug: p for p in game.platforms}
    wanted = {p.slug for p in card.platforms}
    for p in card.platforms:
        row = existing.get(p.slug) or GamePlatform(slug=p.slug)
        row.name, row.release_date, row.metascore, row.critic_count = p.name, p.release_date, p.metascore, p.critic_count
        if p.slug not in existing:
            game.platforms.append(row)
    for slug, row in existing.items():
        if slug not in wanted:
            game.platforms.remove(row)


async def text_counts(session: AsyncSession, game_id: int) -> tuple[int, int]:
    """Reviews the model would read: every critic review with a quote, player reviews of 50+ words."""
    critic = await session.scalar(select(func.count()).select_from(Review).where(
        Review.game_id == game_id, Review.kind == "critic", Review.quote.is_not(None)))
    user = await session.scalar(select(func.count()).select_from(Review).where(
        Review.game_id == game_id, Review.kind == "user", Review.word_count >= SUBSTANTIVE_WORDS))
    return critic or 0, user or 0


async def _insert_reviews(session: AsyncSession, game_id: int, rows: list[ReviewData]) -> int:
    unique = {r.id: r for r in rows}
    values = [{**r.__dict__, "game_id": game_id} for r in unique.values()]
    inserted = 0
    for i in range(0, len(values), 500):
        stmt = (pg_insert(Review).values(values[i:i + 500])
                .on_conflict_do_nothing(index_elements=["id"]).returning(Review.id))
        inserted += len((await session.execute(stmt)).all())
    return inserted


async def _sync_reviews(d: Deps, session: AsyncSession, card: GameCard, full: bool) -> int:
    new = 0
    with_critics = [p for p in card.platforms if p.critic_count]
    plan = {
        # Per-platform critic counts tell which platforms have critics; fall back to all if they are missing.
        "critic": with_critics or (card.platforms if card.critic_count else []),
        "user": card.platforms if card.user_count else [],
    }
    for kind, platforms in plan.items():
        if not platforms:
            continue
        known = None
        if not full:
            known = set((await session.execute(
                select(Review.id).where(Review.game_id == card.id, Review.kind == kind))).scalars()) or None
        for p in platforms:
            raw = await d.mc.reviews(kind, card.slug, p.slug, known)
            new += await _insert_reviews(session, card.id, [parse_review(r, kind) for r in raw])
    return new


async def ingest_game(d: Deps, slug: str, source: str | None = None, force_full: bool = False) -> IngestResult:
    t0 = time.perf_counter()
    s = d.settings
    raw = await d.mc.composer(slug)
    card = parse_composer(raw, s.mc_image_base)
    now = clock.now()

    async with d.sessions() as session:
        game = await session.get(Game, card.id)
        is_new = game is None
        if is_new:
            game = Game(id=card.id, discovered_at=now, sources=[], snapshot_count=0, snapshot_at=now, platforms=[])
            session.add(game)
        changed = is_new or force_full or _fingerprint(card) != _stored_fingerprint(game)
        prev_total = 0 if is_new else game.total_reviews

        for f in CARD_FIELDS:
            setattr(game, f, getattr(card, f))
        # A catalog game that later gets a Metascore is a New Release too.
        for src in {source, "new_releases" if card.metascore is not None else None} - {None}:
            if src not in game.sources:
                game.sources = [*game.sources, src]
        _sync_platforms(game, card)
        await session.execute(
            pg_insert(RawResponse).values(kind="composer", key=slug, body=raw, fetched_at=now)
            .on_conflict_do_update(index_elements=["kind", "key"], set_={"body": raw, "fetched_at": now}))
        await session.flush()

        new_reviews = await _sync_reviews(d, session, card, full=is_new or force_full) if changed else 0

        total = game.total_reviews
        if total > prev_total:
            game.last_growth_at = now
        if not is_new and is_spike(game.snapshot_count, total, s):
            game.hot_until = now + timedelta(hours=s.spike_hot_hours)
            await d.bus.emit("tier.spike", slug=slug, before=game.snapshot_count, now=total)
        if is_new or game.snapshot_at is None or now - game.snapshot_at >= timedelta(hours=24):
            game.snapshot_count, game.snapshot_at = total, now

        tier, next_at = compute_tier(
            TierInput(game.release_date, game.discovered_at, total, game.last_growth_at, game.hot_until), now, s)
        if tier != game.tier:
            await d.bus.emit("tier.change", slug=slug, prev=game.tier, to=tier)
        game.tier, game.next_refresh_at, game.checked_at = tier, next_at, now
        if tier == "archived" and game.archived_at is None:
            game.archived_at = now

        critic_texts, user_texts = await text_counts(session, card.id)
        analyze = needs_analysis(critic_texts=critic_texts, user_texts=user_texts,
                                 analyzed_texts=game.analyzed_review_count, analyzed_at=game.analyzed_at,
                                 now=now, s=s)
        await session.commit()

    await d.bus.emit("card.done", slug=slug, ms=round((time.perf_counter() - t0) * 1000), new=is_new,
                     changed=changed, new_reviews=new_reviews, critic=card.critic_count, user=card.user_count,
                     platforms=len(card.platforms), tier=tier)
    return IngestResult(card.id, slug, is_new, changed, new_reviews, tier, analyze)
