"""Collect a game's documents from every platform into one table. Idempotent: what is stored is not fetched
again, a changed store text replaces the old one and its evidence, and a review link that gave no article is
remembered (an empty "unavailable" document) so it is not downloaded every day."""

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import Settings
from app.models import Document, Evidence, Game, GameLink, Review
from app.sources.critics import Critics
from app.sources.steam import Steam, same_developer, sample

LINK_RETRY = timedelta(days=7)  # a game not found on Steam is looked for again a week later: pages appear late


def digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


async def upsert(session, game_id: int, source: str, kind: str, ext_id: str, text: str, meta: dict | None = None,
                 posted_at: datetime | None = None, now: datetime | None = None) -> bool:
    """Store a document; True when it is new or its text changed (then it is read again)."""
    h = digest(text)
    old = await session.scalar(select(Document).where(Document.source == source, Document.ext_id == ext_id))
    if old is not None:
        if old.text_hash == h:
            return False
        await session.execute(delete(Evidence).where(Evidence.game_id == game_id, Evidence.source == source,
                                                     Evidence.document_ids.any(old.id)))
        old.text, old.text_hash, old.meta, old.extracted_at, old.fetched_at = text, h, meta or {}, None, now
        return True
    await session.execute(pg_insert(Document).values(
        game_id=game_id, source=source, kind=kind, ext_id=ext_id, text=text, meta=meta or {}, text_hash=h,
        posted_at=posted_at, fetched_at=now).on_conflict_do_nothing())
    return True


async def metacritic_docs(session, game: Game, now: datetime) -> int:
    """Texts already in our database: the store description and the reviews Metacritic gave us."""
    n = 0
    if game.description:
        n += await upsert(session, game.id, "mc", "store_text", f"{game.id}:description", game.description, now=now)
    rows = (await session.execute(select(Review).where(Review.game_id == game.id, Review.quote.is_not(None)))).scalars()
    for r in rows:
        meta = {"score": r.score, "publication": r.publication, "url": r.url}
        kind = "critic_quote" if r.kind == "critic" else "user_review"
        n += await upsert(session, game.id, "mc", kind, r.id, r.quote, {k: v for k, v in meta.items() if v}, now=now)
    return n


async def steam_link(session, steam: Steam, game: Game, now: datetime) -> GameLink:
    link = await session.get(GameLink, (game.id, "steam"))
    if link and (link.external_id or link.method == "manual" or now - link.checked_at < LINK_RETRY):
        return link
    appid, info = await steam.resolve(game.title, game.developer)
    method = "title_exact" if appid else "not_found"
    if appid:
        page = await steam.store(appid)
        if not same_developer(page.developers, game.developer):
            # the same title by another studio: a different game, never a link
            appid, method = None, "not_found"
            info = {"rejected": page.name, "developers": page.developers}
        else:
            info = {"name": page.name, "developers": page.developers, "release": page.release, "tags": page.tags}
    values = {"external_id": str(appid) if appid else None, "url": f"https://store.steampowered.com/app/{appid}/"
              if appid else None, "method": method, "info": info, "checked_at": now}
    if link is None:
        link = GameLink(game_id=game.id, source="steam", **values)
        session.add(link)
    else:
        for k, v in values.items():
            setattr(link, k, v)
    return link


async def steam_docs(session, steam: Steam, s: Settings, game: Game, appid: int, link: GameLink,
                     now: datetime) -> dict:
    stats = {}
    page = await steam.store(appid)
    stats["steam_store"] = (
        await upsert(session, game.id, "steam_store", "store_text", f"{appid}:about", page.about, now=now)
        if page.about else 0) + await upsert(session, game.id, "steam_store", "store_meta", f"{appid}:meta",
                                             page.meta, now=now)
    link.info = {**(link.info or {}), "tags": page.tags, "release": page.release}

    known = set((await session.execute(select(Document.ext_id).where(
        Document.game_id == game.id, Document.source == "steam_reviews"))).scalars())
    fresh, totals = await steam.reviews(appid, s.steam_review_pages if not known else 2, known)
    # first collection: a sample across recommend x hours; after that, the newest keep flowing in, a quarter at a time
    room = s.steam_reviews_cap - len(known) if len(known) < s.steam_reviews_cap else s.steam_reviews_cap // 4
    added = 0
    for r in sample(fresh, max(0, room)):
        added += await upsert(session, game.id, "steam_reviews", "user_review", r.id, r.text,
                              {"voted_up": r.voted_up, "hours": r.hours, "lang": r.lang,
                               "early_access": r.early_access}, posted_at=r.posted_at, now=now)
    stats["steam_reviews"] = added
    link.info = {**link.info, "reviews": totals}

    added = 0
    for n in await steam.news(appid):
        added += await upsert(session, game.id, "steam_news", "dev_news", n["gid"], f"{n['title']}\n{n['text']}",
                              posted_at=n["date"], now=now)
    stats["steam_news"] = added
    return stats


async def critic_docs(session, critics: Critics, s: Settings, game: Game, now: datetime) -> int:
    tried = set((await session.execute(select(Document.ext_id).where(
        Document.game_id == game.id, Document.source == "critic_full"))).scalars())
    rows = (await session.execute(select(Review).where(
        Review.game_id == game.id, Review.kind == "critic", Review.url.is_not(None))
        .order_by(Review.date.desc().nulls_last()).limit(s.critic_max_reviews))).scalars().all()
    added = 0
    for r in rows:
        if r.url in tried:
            continue
        try:
            text = await critics.full_text(r.url)
        except Exception:  # noqa: BLE001  one site failing is an unavailable article, not a failed game
            text = None
        meta = {"score": r.score, "publication": r.publication, "url": r.url}
        if text:
            added += await upsert(session, game.id, "critic_full", "critic_review", r.url, text, meta, now=now)
        else:
            await upsert(session, game.id, "critic_full", "unavailable", r.url, "", meta, now=now)
    return added
