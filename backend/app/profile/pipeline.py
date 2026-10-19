"""One enrichment pass for one game: collect from every platform, extract evidence from new documents, recompute
fullness, build the profile once there is enough and again when the evidence has grown, plan the next pass."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from app.core import clock
from app.core.config import Settings
from app.models import Document, Evidence, Game, GameLink, GameProfile, ProfileCall, Review
from app.profile import build as builder
from app.profile.collect import critic_docs, metacritic_docs, steam_docs, steam_link
from app.profile.evidence import Doc, extract
from app.profile.fullness import Point, band, fullness, missing
from app.sources.critics import Critics
from app.sources.steam import Steam

log = logging.getLogger(__name__)
SOURCES = ["mc", "steam_store", "steam_reviews", "steam_news", "critic_full"]


@dataclass
class EnrichResult:
    slug: str
    collected: dict = field(default_factory=dict)
    extracted: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fullness: int = 0
    built: bool = False
    errors: dict = field(default_factory=dict)


def next_pass(game: Game, now: datetime, s: Settings) -> datetime | None:
    if game.archived_at is not None:
        return None
    hours = {"hot": s.enrich_hot_hours, "warm": s.enrich_warm_hours}.get(game.tier, s.enrich_cool_hours)
    return now + timedelta(hours=hours)


async def due_games(session, limit: int, now: datetime) -> list[int]:
    """Games never enriched or due again; the New Releases top first, then the most reviewed and the newest."""
    q = (select(Game.id).outerjoin(GameProfile, GameProfile.game_id == Game.id)
         .where(Game.archived_at.is_(None),
                or_(GameProfile.game_id.is_(None), GameProfile.next_enrich_at <= now))
         .order_by(Game.top_rank.asc().nulls_last(), (Game.critic_count + Game.user_count).desc(),
                   Game.release_date.desc().nulls_last())
         .limit(limit))
    return list((await session.execute(q)).scalars())


async def collect_all(sessions, steam: Steam, critics: Critics, s: Settings, game_id: int,
                      res: EnrichResult) -> None:
    now = clock.now()
    async with sessions() as session:
        game = await session.get(Game, game_id)
        res.collected["mc"] = await metacritic_docs(session, game, now)
        await session.commit()
    for name, step in (("steam", "steam"), ("critic_full", "critics")):
        try:
            async with sessions() as session:
                game = await session.get(Game, game_id)
                if step == "steam":
                    link = await steam_link(session, steam, game, now)
                    if link.external_id:
                        res.collected.update(await steam_docs(session, steam, s, game, int(link.external_id), link,
                                                              now))
                else:
                    res.collected["critic_full"] = await critic_docs(session, critics, s, game, now)
                await session.commit()
        except Exception as e:  # one platform failing keeps what the others gave
            log.exception("collect %s failed: %s", name, game_id)
            res.errors[name] = f"{type(e).__name__}: {e}"[:300]


async def extract_new(sessions, provider, s: Settings, game: Game, res: EnrichResult) -> None:
    for source in SOURCES:
        async with sessions() as session:
            rows = (await session.execute(select(Document).where(
                Document.game_id == game.id, Document.source == source, Document.extracted_at.is_(None),
                Document.text != "").order_by(Document.id))).scalars().all()
        if not rows:
            continue
        try:
            out = await extract(provider, game.title, source, [Doc(r.id, r.kind, r.text, r.meta) for r in rows],
                                model=s.llm_model, thinking_level=s.llm_thinking_level)
        except Exception as e:  # noqa: BLE001  a daily limit or a broken answer: these documents wait for next pass
            res.errors[f"extract:{source}"] = f"{type(e).__name__}: {e}"[:300]
            continue
        now = clock.now()
        async with sessions() as session:
            for ev in out.evidence:
                session.add(Evidence(game_id=game.id, source=source, **ev))
            for tin, tout in out.calls:
                session.add(ProfileCall(game_id=game.id, stage="extract", source=source, model=s.llm_model,
                                        tokens_in=tin, tokens_out=tout))
            for r in rows:
                doc = await session.get(Document, r.id)
                doc.extracted_at = now
            await session.commit()
        res.extracted += len(out.evidence)
        res.tokens_in += sum(c[0] for c in out.calls)
        res.tokens_out += sum(c[1] for c in out.calls)


async def score(session, game: Game, s: Settings) -> tuple[int, dict, dict, list[dict]]:
    ev = (await session.execute(select(Evidence).where(Evidence.game_id == game.id))).scalars().all()
    total, factors = fullness([Point(e.factor, e.source, e.specific, e.polarity, e.who, e.is_claim) for e in ev])
    docs = dict((await session.execute(
        select(Document.source, func.count()).where(Document.game_id == game.id, Document.text != "")
        .group_by(Document.source))).all())
    by_source = {src: {"documents": docs.get(src, 0), "evidence": sum(e.source == src for e in ev)}
                 for src in SOURCES if docs.get(src) or any(e.source == src for e in ev)}
    rows = [{"id": e.id, "factor": e.factor, "source": e.source, "claim": e.claim, "specific": e.specific,
             "polarity": e.polarity, "who": e.who, "is_claim": e.is_claim} for e in ev]
    return total, {str(f): x for f, x in factors.items()}, by_source, rows


async def enrich_game(sessions, provider, steam: Steam, critics: Critics, s: Settings, game_id: int) -> EnrichResult:
    async with sessions() as session:
        game = await session.get(Game, game_id)
    res = EnrichResult(slug=game.slug)
    await collect_all(sessions, steam, critics, s, game_id, res)
    if provider is not None:
        await extract_new(sessions, provider, s, game, res)

    now = clock.now()
    async with sessions() as session:
        game = await session.get(Game, game_id)
        total, factors, by_source, evidence = await score(session, game, s)
        prof = await session.get(GameProfile, game_id) or GameProfile(game_id=game_id, profile_evidence=0)
        session.add(prof)
        prof.fullness, prof.factors, prof.sources, prof.evidence_count = total, factors, by_source, len(evidence)
        prof.enriched_at, prof.next_enrich_at = now, next_pass(game, now, s)
        prof.error = "; ".join(f"{k}: {v}" for k, v in res.errors.items()) or None
        grown = len(evidence) >= max(1, prof.profile_evidence) * (1 + s.profile_rebuild_growth)
        want = provider is not None and total >= s.profile_min_fullness and (prof.payload is None or grown)
        await session.commit()
    res.fullness = total
    if not want:
        return res

    try:
        payload, dropped, r = await builder.build(provider, game.title, game.genres or [], evidence,
                                                  {int(f): x for f, x in factors.items()}, model=s.llm_model,
                                                  thinking_level=s.llm_thinking_level)
    except Exception as e:  # noqa: BLE001
        res.errors["profile"] = f"{type(e).__name__}: {e}"[:300]
        return res
    async with sessions() as session:
        prof = await session.get(GameProfile, game_id)
        prof.payload, prof.profile_evidence, prof.model, prof.profiled_at = payload, len(evidence), r.model, clock.now()
        session.add(ProfileCall(game_id=game_id, stage="profile", model=r.model, tokens_in=r.tokens_in,
                                tokens_out=r.tokens_out))
        await session.commit()
    res.built, res.tokens_in, res.tokens_out = True, res.tokens_in + r.tokens_in, res.tokens_out + r.tokens_out
    if dropped:
        res.errors["profile_refs_dropped"] = str(dropped)
    return res


async def card(session, game: Game, threshold: int) -> dict | None:
    """What the game card shows: fullness with its band and what is missing, the profile, the evidence it cites."""
    prof = await session.get(GameProfile, game.id)
    if prof is None:
        return None
    link = await session.get(GameLink, (game.id, "steam"))
    critic_links = await session.scalar(select(func.count()).select_from(Review).where(
        Review.game_id == game.id, Review.kind == "critic", Review.url.is_not(None))) or 0
    docs = {k: v["documents"] for k, v in (prof.sources or {}).items()}
    code, label = band(prof.fullness)
    cited = sorted({i for f in (prof.payload or {}).values() for p in f["points"] for i in p["evidence"]})
    rows = (await session.execute(select(Evidence).where(Evidence.id.in_(cited)))).scalars().all() if cited else []
    doc_ids = sorted({d for e in rows for d in e.document_ids})
    urls = dict((await session.execute(select(Document.id, Document.meta["url"].astext).where(
        Document.id.in_(doc_ids)))).all()) if doc_ids else {}
    return {
        "fullness": prof.fullness, "band": code, "band_label": label, "threshold": threshold,
        "factors": {f: {"score": round(100 * x["score"]), "kinds": x["kinds"]} for f, x in (prof.factors or {}).items()},
        "sources": prof.sources, "evidence_count": prof.evidence_count,
        "steam": {"appid": link.external_id, "url": link.url, "reviews": (link.info or {}).get("reviews"),
                  "tags": (link.info or {}).get("tags")} if link and link.external_id else None,
        "missing": missing(prof.factors or {}, docs, bool(link.external_id) if link else None, critic_links),
        "profile": prof.payload, "model": prof.model, "profiled_at": prof.profiled_at, "enriched_at": prof.enriched_at,
        "evidence": {e.id: {"claim": e.claim, "source": e.source, "polarity": e.polarity, "is_claim": e.is_claim,
                            "urls": [u for d in e.document_ids if (u := urls.get(d))][:3]} for e in rows},
    }
