import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import and_, desc, func, nulls_last, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import presence
from app.core import clock
from app.core.config import get_settings
from app.core.db import SessionLocal, init_db
from app.models import (
    Analysis,
    DeepAnalysis,
    Game,
    GameFacets,
    GamePlatform,
    PipelineRun,
    Review,
    SimilarGame,
    TopRelease,
)
from app.pipeline.quota import PENDING_STATUSES, deep_quota
from app.profile.pipeline import card as profile_card

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.arq.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="GameAnalytics", lifespan=lifespan)


async def db():
    async with SessionLocal() as session:
        yield session


def game_brief(g: Game) -> dict:
    return {
        "id": g.id, "slug": g.slug, "title": g.title, "cover_url": g.cover_url,
        "release_date": g.release_date, "genres": g.genres, "developer": g.developer,
        "platforms": sorted(p.name for p in g.platforms),
        "metascore": g.metascore, "critic_count": g.critic_count,
        "userscore": g.userscore, "user_count": g.user_count,
        "tier": g.tier, "analyzed": g.analyzed_at is not None, "must_play": g.must_play,
        "top_rank": g.top_rank,
    }


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/platforms")
async def platforms(session: AsyncSession = Depends(db)):
    rows = await session.execute(
        select(GamePlatform.name, func.count()).group_by(GamePlatform.name).order_by(desc(func.count())))
    return [{"name": n, "games": c} for n, c in rows]


SORTS = {"metascore": Game.metascore, "userscore": Game.userscore, "release": Game.release_date,
         "reviews": Game.critic_count + Game.user_count}


@app.get("/api/games")
async def list_games(
    q: str | None = None,
    platform: list[str] = Query(default=[]),
    sort: Literal["metascore", "userscore", "release", "reviews"] = "release",
    order: Literal["asc", "desc"] = "desc",
    archived: bool = False,
    top: bool = False,
    limit: int = Query(48, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(db),
):
    where = []
    if not archived:
        where.append(Game.archived_at.is_(None))
    if q:
        q = q[:200]  # longer is not a title; trigram similarity on megabytes is just load
        literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # "%" means a percent sign
        where.append(or_(Game.title.ilike(f"%{literal}%", escape="\\"), func.similarity(Game.title, q) > 0.3))
    if platform:
        where.append(Game.platforms.any(GamePlatform.name.in_(platform)))
    if top:  # Metacritic's "New Releases" block, in its own order
        where.append(Game.top_rank.is_not(None))
    col = SORTS[sort]
    ordering = Game.top_rank.asc() if top else nulls_last(desc(col) if order == "desc" else col.asc())
    stmt = select(Game).where(and_(*where)).order_by(ordering, Game.id)
    total = await session.scalar(select(func.count()).select_from(Game).where(and_(*where)))
    games = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [game_brief(g) for g in games]}


@app.get("/api/top")
async def top_by_day(day: date | None = None, session: AsyncSession = Depends(db)):
    """The "New Releases" block as last seen on `day` (UTC); today by default."""
    day = day or clock.now().date()
    rows = (await session.execute(select(TopRelease, Game).join(Game, Game.id == TopRelease.game_id)
                                  .where(TopRelease.day == day).order_by(TopRelease.rank))).all()
    return {"day": day, "observed_at": rows[0][0].observed_at if rows else None,
            "items": [{**game_brief(g), "rank": t.rank, "metascore_then": t.metascore} for t, g in rows]}


async def _game_or_404(session: AsyncSession, slug: str) -> Game:
    game = await session.scalar(select(Game).where(Game.slug == slug))
    if not game:
        raise HTTPException(404, f"Игра {slug} не найдена")
    return game


async def _similar(session: AsyncSession, g: Game) -> dict:
    """Precomputed neighbours by essence (gameplay 0.5, tags 0.3, world 0.2); until a game has them, same genre."""
    rows = (await session.execute(
        select(SimilarGame, Game).join(Game, Game.id == SimilarGame.other_id)
        .where(SimilarGame.game_id == g.id).order_by(SimilarGame.kind, SimilarGame.rank))).all()
    if rows:
        out: dict[str, list] = {"similar": [], "series": []}
        for sim, other in rows:
            out[sim.kind].append({**game_brief(other), "score": sim.score, "reasons": sim.reasons})
        return {"method": "essence", "items": out["similar"], "series": out["series"]}
    if await session.get(GameFacets, g.id) is not None:  # read by essence, nothing close enough: say so
        return {"method": "essence", "items": [], "series": []}
    if not g.genres:
        return {"method": "none", "items": [], "series": []}
    rows = await session.execute(
        select(Game).where(Game.id != g.id, Game.genres.overlap(g.genres))
        .order_by(nulls_last(desc(Game.metascore))).limit(8))
    return {"method": "genre", "items": [game_brief(x) for x in rows.scalars()], "series": []}


@app.get("/api/games/{slug}")
async def game_detail(slug: str, session: AsyncSession = Depends(db)):
    g = await _game_or_404(session, slug)
    analyses = (await session.execute(
        select(Analysis).where(Analysis.game_id == g.id).order_by(desc(Analysis.created_at)))).scalars().all()
    latest: dict[str, dict] = {}
    for a in analyses:
        latest.setdefault(a.kind, {"payload": a.payload, "model": a.model, "reviews_seen": a.reviews_seen,
                                   "created_at": a.created_at})
    stored = dict((await session.execute(
        select(Review.kind, func.count()).where(Review.game_id == g.id).group_by(Review.kind))).all())
    deep = (await session.execute(
        select(DeepAnalysis).where(DeepAnalysis.game_id == g.id).order_by(desc(DeepAnalysis.created_at))
        .limit(1))).scalar_one_or_none()
    return {
        **game_brief(g),
        "description": g.description, "publisher": g.publisher, "rating": g.rating,
        "video": {"title": g.video_title, "embed_url": g.video_embed_url} if g.video_embed_url else None,
        "platform_scores": [{"name": p.name, "slug": p.slug, "metascore": p.metascore,
                             "critic_count": p.critic_count, "release_date": p.release_date}
                            for p in sorted(g.platforms, key=lambda p: -(p.critic_count or 0))],
        "stored_reviews": {"critic": stored.get("critic", 0), "user": stored.get("user", 0)},
        "analysis": latest,
        "similar": await _similar(session, g),
        "profile": await profile_card(session, g, settings.profile_min_fullness),
        "light_essence": facets.essence if (facets := await session.get(GameFacets, g.id)) else None,
        "deep": deep and {"id": deep.id, "status": deep.status, "error": deep.error, "facts": deep.facts,
                          "video_title": deep.video_title, "updated_at": deep.updated_at},
        "checked_at": g.checked_at, "next_refresh_at": g.next_refresh_at, "archived_at": g.archived_at,
    }


@app.get("/api/games/{slug}/reviews")
async def game_reviews(
    slug: str,
    kind: Literal["critic", "user"] = "critic",
    platform: str | None = None,
    sort: Literal["date", "score"] = "date",
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(db),
):
    g = await _game_or_404(session, slug)
    where = [Review.game_id == g.id, Review.kind == kind]
    if platform:
        where.append(Review.platform == platform)
    col = Review.date if sort == "date" else Review.score
    total = await session.scalar(select(func.count()).select_from(Review).where(*where))
    rows = (await session.execute(select(Review).where(*where).order_by(nulls_last(desc(col)), Review.id)
                                  .limit(limit).offset(offset))).scalars()
    return {"total": total, "items": [
        {"id": r.id, "platform": r.platform, "author": r.author, "publication": r.publication, "score": r.score,
         "date": r.date, "quote": r.quote, "url": r.url, "spoiler": r.spoiler} for r in rows]}


def client_ip(request: Request) -> str:
    """The visitor's address: nginx in front of the api passes it as X-Real-IP (the api itself is not exposed)."""
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown"))[:64]


def client_id(request: Request) -> str:
    """Anonymous id the web app keeps in localStorage; the address is a fallback for other clients."""
    cid = (request.headers.get("x-client-id") or "").strip()[:64]
    return cid or f"ip:{request.client.host if request.client else 'unknown'}"


@app.get("/api/deep/quota")
async def deep_quota_status(request: Request, session: AsyncSession = Depends(db)):
    quota = await deep_quota(session, settings, client_id(request), clock.now(), client_ip(request))
    await session.commit()  # keeps the lost analyses it released
    return quota.as_dict()


@app.post("/api/games/{slug}/deep-analyze", status_code=202)
async def start_deep(slug: str, request: Request, session: AsyncSession = Depends(db)):
    g = await _game_or_404(session, slug)
    now = clock.now()
    # One analysis per game serves everyone: a pending or recent one is returned and costs no quota.
    existing = await session.scalar(
        select(DeepAnalysis).where(
            DeepAnalysis.game_id == g.id,
            or_(and_(DeepAnalysis.status.in_(PENDING_STATUSES),  # a job lost in a restart must not block forever
                     DeepAnalysis.updated_at >= now - timedelta(minutes=30)),
                and_(DeepAnalysis.status == "done",
                     DeepAnalysis.updated_at >= now - timedelta(days=settings.deep_reuse_days))))
        .order_by(desc(DeepAnalysis.created_at)).limit(1))
    if existing:
        return {"id": existing.id, "status": existing.status, "cached": True}

    user = client_id(request)
    await session.execute(text("SELECT pg_advisory_xact_lock(4343)"))  # two clicks at once must not both pass
    quota = await deep_quota(session, settings, user, now, client_ip(request))
    if quota.reason:
        raise HTTPException(429, detail=quota.reason)
    row = DeepAnalysis(game_id=g.id, status="queued", requested_by=user, requested_ip=client_ip(request))
    session.add(row)
    await session.commit()
    try:
        await request.app.state.arq.enqueue_job("deep_analyze", row.id)
    except Exception as e:  # noqa: BLE001 - the queue is down: give the slot back instead of a row queued forever
        row.status, row.error = "lost", f"Очередь задач недоступна: {type(e).__name__}"
        await session.commit()
        raise HTTPException(503, detail="Очередь задач недоступна, попробуйте позже") from None
    return {"id": row.id, "status": row.status, "cached": False, "left": quota.left - 1}


class RunRequest(BaseModel):
    # The first fill of an empty database (bootstrap) is a one-off: only `python -m app.cli enqueue bootstrap`.
    kind: Literal["hourly", "sweep"]


ADMIN_TRIES = 5  # wrong passwords from one address before a pause
ADMIN_PAUSE_S = 600


async def require_admin(request: Request) -> None:
    """DevPanel's run buttons ask for the password; it comes in X-Admin-Password. Guessing is slowed down to
    ADMIN_TRIES wrong answers per address every ADMIN_PAUSE_S."""
    if not settings.admin_password:
        return
    redis: Redis = request.app.state.redis
    key = f"admin:fail:{client_ip(request)}"
    if int(await redis.get(key) or 0) >= ADMIN_TRIES:
        raise HTTPException(429, "Слишком много неверных попыток. Попробуйте через 10 минут.")
    given = request.headers.get("x-admin-password", "")
    if not hmac.compare_digest(given.encode(), settings.admin_password.encode()):
        async with redis.pipeline(transaction=False) as p:
            p.incr(key)
            p.expire(key, ADMIN_PAUSE_S)
            await p.execute()
        raise HTTPException(401, "Неверный пароль" if given else "Нужен пароль")


@app.post("/api/pipeline/run", status_code=202, dependencies=[Depends(require_admin)])
async def start_run(body: RunRequest, request: Request):
    job = await request.app.state.arq.enqueue_job(f"run_{body.kind}", _job_id=f"manual:{body.kind}")
    return {"queued": job is not None}


@app.get("/api/pipeline/runs")
async def runs(limit: int = Query(20, ge=1, le=200), session: AsyncSession = Depends(db)):
    rows = (await session.execute(select(PipelineRun).order_by(desc(PipelineRun.started_at)).limit(limit))).scalars()
    return [{"id": r.id, "kind": r.kind, "status": r.status, "started_at": r.started_at,
             "finished_at": r.finished_at, "stats": r.stats} for r in rows]


@app.get("/api/pipeline/stats")
async def pipeline_stats(request: Request, session: AsyncSession = Depends(db)):
    tiers = dict((await session.execute(select(Game.tier, func.count()).group_by(Game.tier))).all())
    now = clock.now()
    due_hour = await session.scalar(select(func.count()).select_from(Game).where(
        Game.next_refresh_at <= now + timedelta(hours=1), Game.archived_at.is_(None)))
    reviews = dict((await session.execute(select(Review.kind, func.count()).group_by(Review.kind))).all())
    queued = await request.app.state.redis.zcard("arq:queue")
    return {"tiers": tiers, "due_next_hour": due_hour, "reviews": reviews, "queued_jobs": queued,
            "rate_limit_rps": settings.rate_limit_rps, "admin_required": bool(settings.admin_password)}


async def _presence_body(request: Request) -> tuple[str, dict] | None:
    """Pings come as JSON, also from sendBeacon on tab close, which may label it text/plain."""
    try:
        body = await request.json()
    except ValueError:
        return None
    client = body.get("id") if isinstance(body, dict) else None
    if not isinstance(client, str) or not presence.ID_RE.match(client):
        return None
    return client, body


@app.post("/api/presence", status_code=204)
async def presence_ping(request: Request):
    got = await _presence_body(request)
    if got is None:
        raise HTTPException(422, "bad presence ping")
    client, body = got
    path = body.get("path") if isinstance(body.get("path"), str) else "/"
    try:
        await presence.ping(request.app.state.redis, client, presence.page_of(path[:200], body.get("play") is True))
    except (RedisConnectionError, RedisTimeoutError):
        pass  # a missed ping is not worth an error in the visitor's console


@app.post("/api/presence/leave", status_code=204)
async def presence_leave(request: Request):
    got = await _presence_body(request)
    if got is None:
        return
    try:
        await presence.leave(request.app.state.redis, got[0])
    except (RedisConnectionError, RedisTimeoutError):
        pass


@app.get("/api/presence")
async def presence_now(request: Request, days: int = Query(14, ge=1, le=presence.HISTORY_DAYS)):
    try:
        return await presence.snapshot(request.app.state.redis, days)
    except (RedisConnectionError, RedisTimeoutError):
        raise HTTPException(503, "presence unavailable") from None


@app.get("/api/events/stream")
async def events_stream(request: Request, history: int = Query(300, ge=0, le=2000)):
    """Server-Sent Events: the last `history` events, then live ones. Resumes from Last-Event-ID.
    While Redis is unreachable the stream stays open with keepalives and picks up again once it is back,
    instead of dropping the connection into a reconnect loop."""
    redis: Redis = request.app.state.redis
    stream = settings.events_stream

    async def gen():
        last_id = request.headers.get("last-event-id")
        yield "retry: 5000\n\n"
        while not await request.is_disconnected():
            try:
                if not last_id:
                    backlog = await redis.xrevrange(stream, count=history) if history else []
                    for eid, fields in reversed(backlog):
                        yield f"id: {eid}\ndata: {fields['data']}\n\n"
                    last_id = backlog[0][0] if backlog else "$"
                res = await redis.xread({stream: last_id}, block=15000, count=500)
            except (RedisConnectionError, RedisTimeoutError):
                yield ": events unavailable, retrying\n\n"
                await asyncio.sleep(5)
                continue
            if not res:
                yield ": keepalive\n\n"
                continue
            for eid, fields in res[0][1]:
                last_id = eid
                yield f"id: {eid}\ndata: {fields['data']}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
