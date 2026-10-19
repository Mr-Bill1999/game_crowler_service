"""arq worker: hourly cycle, bootstrap, daily sweep, analysis and deep-analyze jobs."""

import asyncio
import logging
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.ai.analyze import essence, load_reviews
from app.ai.deep import absolute_facts, run_deep, search_queries
from app.ai.gemini import GeminiProvider
from app.ai.lifecycle import analyze_kind, latest_analyses, metacritic_user_summary
from app.core import clock
from app.core.config import get_settings
from app.core.db import SessionLocal, init_db
from app.events.bus import EventBus, current_run_id
from app.models import Analysis, DeepAnalysis, Game, PipelineRun, SimilarGame
from app.pipeline.discovery import discover_bootstrap, discover_hourly, discover_sweep
from app.pipeline.facets import build_essences, embed_pending, refinish_all, stale_games
from app.pipeline.ingest import Deps, IngestResult, ingest_game
from app.pipeline.quota import FREE_STATUSES
from app.pipeline.similarity import rebuild
from app.pipeline.top import record_top
from app.scraper.client import MCClient
from app.scraper.metacritic import Metacritic
from app.sources.youtube import YouTube, YouTubeError, pick_clips, pick_video

log = logging.getLogger(__name__)
RUN_TIMEOUT = 55 * 60
ctx_owner: ContextVar[str] = ContextVar("run_owner", default="worker")
INGEST_CONCURRENCY = 4  # games in flight; HTTP pacing is the rate limiter's job


async def startup(ctx: dict) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    await init_db()
    redis = Redis.from_url(s.redis_url, decode_responses=True)
    bus = EventBus(redis, s.events_stream, s.events_maxlen)
    client = MCClient(s, bus)
    ctx.update(deps=Deps(s, Metacritic(client, s), bus, SessionLocal), _client=client, _bus_redis=redis)
    if not ctx.get("cli"):  # the worker starting: none of its runs can still be in progress
        async with SessionLocal() as session:
            if lost := await close_lost_runs(session, datetime.now(UTC), worker_restarted=True):
                await bus.emit("run.interrupted", "error", count=lost, reason="worker restarted")
        # arq marks a running job "in progress" until its timeout (55 min for a cycle); a killed worker leaves the
        # marks behind and the jobs wait out that hour. One worker runs this queue, so before it takes any job,
        # every mark belongs to a dead process: drop them and the jobs run again now (they are idempotent).
        stale = [k async for k in ctx["redis"].scan_iter(match="arq:in-progress:*")]
        if stale:
            await ctx["redis"].delete(*stale)
            await bus.emit("worker.recovered", "retry", jobs=len(stale))
    if s.llm_provider == "gemini" and s.gemini_keys:
        ctx["llm"] = GeminiProvider(s.gemini_keys, s.llm_model, rpm=s.llm_rpm, concurrency=s.llm_concurrency)
    await bus.emit("worker.start", rps=s.rate_limit_rps, concurrency=s.max_concurrency,
                   llm=s.llm_model if "llm" in ctx else None, llm_keys=len(s.gemini_keys))


async def shutdown(ctx: dict) -> None:
    await ctx["_client"].aclose()
    await ctx["_bus_redis"].aclose()


async def close_lost_runs(session, now: datetime, *, worker_restarted: bool = False) -> int:
    """Runs a killed process left "running". Past the job timeout none can still be alive; and when the worker
    itself starts, none of its own can be (one worker runs the cycles). Runs started by hand from the CLI next to a
    busy worker are only closed by the timeout."""
    lost = PipelineRun.started_at < now - timedelta(seconds=RUN_TIMEOUT + 300)
    if worker_restarted:
        lost = lost | (PipelineRun.owner == "worker")
    r = await session.execute(update(PipelineRun).where(PipelineRun.status == "running", lost)
                              .values(status="interrupted", finished_at=now))
    await session.commit()
    return r.rowcount


async def _start_run(d: Deps, kind: str) -> str:
    run_id = f"{kind}-{datetime.now(UTC):%m%d-%H%M%S}"
    current_run_id.set(run_id)
    async with d.sessions() as session:
        if lost := await close_lost_runs(session, datetime.now(UTC)):
            await d.bus.emit("run.interrupted", "error", count=lost)
        session.add(PipelineRun(id=run_id, kind=kind, stats={}, owner=ctx_owner.get()))
        await session.commit()
    await d.bus.emit("run.start", kind=kind)
    return run_id


async def _finish_run(d: Deps, run_id: str, status: str, stats: dict) -> None:
    async with d.sessions() as session:
        await session.execute(update(PipelineRun).where(PipelineRun.id == run_id)
                              .values(status=status, finished_at=datetime.now(UTC), stats=stats))
        await session.commit()
    await d.bus.emit("run.done", status={"done": "ok", "partial": "retry"}.get(status, "error"), result=status, **stats)


async def _ingest_many(ctx: dict, items: list[tuple[str, str | None]]) -> dict:
    d: Deps = ctx["deps"]
    gate = asyncio.Semaphore(INGEST_CONCURRENCY)

    async def one(slug: str, source: str | None) -> IngestResult | None:
        async with gate:
            try:
                return await ingest_game(d, slug, source)
            except Exception as e:
                log.exception("ingest failed: %s", slug)
                await d.bus.emit("card.error", "error", slug=slug, error=f"{type(e).__name__}: {e}"[:300])
                return None

    t0 = datetime.now(UTC)
    results = await asyncio.gather(*(one(slug, src) for slug, src in items))
    ok = [r for r in results if r]
    for r in ok:
        if r.analyze:
            await ctx["redis"].enqueue_job("analyze_game", r.game_id, current_run_id.get(),
                                           _job_id=f"analyze:{r.game_id}")
    return {
        "games": len(items),
        "ok": len(ok),
        "errors": len(items) - len(ok),
        "new_games": sum(r.is_new for r in ok),
        "changed": sum(r.changed for r in ok),
        "new_reviews": sum(r.new_reviews for r in ok),
        "to_analyze": sum(r.analyze for r in ok),
        "tiers": dict(Counter(r.tier for r in ok)),
        "seconds": round((datetime.now(UTC) - t0).total_seconds(), 1),
    }


def _reason(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


async def _run(ctx: dict, kind: str, work) -> dict:
    d: Deps = ctx["deps"]
    run_id = await _start_run(d, kind)
    try:
        stats = await work(d)
    except Exception as e:
        await _finish_run(d, run_id, "error", {"error": f"{type(e).__name__}: {e}"[:300]})
        raise
    await _finish_run(d, run_id, "partial" if stats.get("failed") else "done", stats)
    await ctx["redis"].enqueue_job("update_facets", _job_id="facets")  # new games need essences and neighbours
    return stats


async def run_bootstrap(ctx: dict, days: int | None = None) -> dict:
    async def work(d: Deps) -> dict:
        window = days or d.settings.bootstrap_days
        async with d.sessions() as session:
            items = await discover_bootstrap(d.mc, session, d.settings, d.bus, clock.now().date(), window)
        return {"window_days": window, **await _ingest_many(ctx, [(i.slug, "new_releases") for i in items])}

    return await _run(ctx, "bootstrap", work)


async def run_hourly(ctx: dict) -> dict:
    """Every hour, from an empty database too: (1) the 20 games of Metacritic's "New Releases" block, ingested first;
    (2) the catalog and New Releases windows of the last three days; (3) games whose refresh is due."""

    async def work(d: Deps) -> dict:
        now = clock.now()
        failed: dict[str, str] = {}
        # Each stage stands on its own: a failing top-20 request must not cost the catalog and the refreshes.
        top = []
        try:
            top = await d.mc.new_releases_top()
        except Exception as e:  # noqa: BLE001
            failed["top"] = _reason(e)
            await d.bus.emit("top.recorded", "error", error=failed["top"])
        top_stats = await _ingest_many(ctx, [(i.slug, "new_releases") for i in top])
        change = {"entered": [], "left": []}
        if top:  # an empty answer would clear yesterday's top: keep it until Metacritic answers again
            async with d.sessions() as session:
                change = await record_top(session, top, now)
            await d.bus.emit("top.recorded", size=change["size"], stored=change["stored"],
                             entered=change["entered"] or None, left=change["left"] or None)

        top_slugs = {i.slug for i in top}
        found: dict = {}
        try:
            async with d.sessions() as session:
                found = await discover_hourly(d.mc, session, d.settings, d.bus, now.date())
        except Exception as e:  # noqa: BLE001
            failed["discovery"] = _reason(e)
        async with d.sessions() as session:
            due = list((await session.execute(
                select(Game.slug).where(Game.next_refresh_at <= now, Game.archived_at.is_(None))
                .order_by(Game.next_refresh_at).limit(2000))).scalars())
        new = [(i.slug, src) for src, items in found.items() for i in items if i.slug not in top_slugs]
        fresh = {slug for slug, _ in new} | top_slugs
        refresh = [(slug, None) for slug in due if slug not in fresh]
        await d.bus.emit("refresh.due", count=len(refresh))
        stats = await _ingest_many(ctx, new + refresh)
        out = {"top": {"games": top_stats["games"], "new_games": top_stats["new_games"],
                       "entered": len(change["entered"]), "left": len(change["left"])},
               "discovered": {src: len(v) for src, v in found.items()}, "refreshed": len(refresh), **stats,
               "to_analyze": stats["to_analyze"] + top_stats["to_analyze"]}
        if failed:
            out["failed"] = failed
        return out

    return await _run(ctx, "hourly", work)


async def run_sweep(ctx: dict) -> dict:
    async def work(d: Deps) -> dict:
        async with d.sessions() as session:
            items = await discover_sweep(d.mc, session, d.settings, d.bus, clock.now().date())
        return await _ingest_many(ctx, [(i.slug, "catalog") for i in items])

    return await _run(ctx, "sweep", work)


async def analyze_game(ctx: dict, game_id: int, run_id: str | None = None) -> None:
    """First read or incremental update of the critic and player summaries, then the essence if either changed."""
    d: Deps = ctx["deps"]
    s = d.settings
    current_run_id.set(run_id)
    provider = ctx.get("llm")
    if provider is None:
        await d.bus.emit("ai.skipped", "skip", game_id=game_id, reason="LLM-провайдер не настроен (GA_LLM_PROVIDER)")
        return
    async with d.sessions() as session:
        game = await session.get(Game, game_id)
        if game is None:
            return
        reviews = {k: await load_reviews(session, game_id, k) for k in ("critic", "user")}
        prev = await latest_analyses(session, game_id)
        user_mc = await metacritic_user_summary(session, game.slug)
    await d.bus.emit("ai.start", slug=game.slug, model=s.llm_model, critic=len(reviews["critic"]),
                     user=len(reviews["user"]))

    runs = await asyncio.gather(*(
        analyze_kind(provider, game, kind, reviews[kind], prev.get(kind), s.llm_model, s.llm_thinking_level,
                     user_mc if kind == "user" else None)
        for kind in ("critic", "user")))
    by_kind = {r.kind: r for r in runs}
    changed = any(r.data for r in runs)
    ess = None
    if changed or ("essence" not in prev and any(k in prev for k in ("critic", "user"))):
        latest = {k: (by_kind[k].data or (prev[k].payload if k in prev else None)) for k in ("critic", "user")}
        ess = await essence(provider, game, latest["critic"], latest["user"], s.llm_model, s.llm_thinking_level)

    now = clock.now()
    async with d.sessions() as session:
        game = await session.get(Game, game_id)
        for r in runs:
            if r.data:
                session.add(Analysis(game_id=game_id, kind=r.kind, mode=r.mode, provider=provider.name,
                                     model=s.llm_model, payload=r.data, reviews_seen=r.read, texts_seen=r.texts_seen,
                                     watermark=r.watermark, tokens_in=r.tokens_in, tokens_out=r.tokens_out,
                                     cost_usd=r.cost_usd))
            elif r.mode == "skip" and r.kind in prev and r.watermark and r.watermark != prev[r.kind].watermark:
                prev_row = await session.get(Analysis, prev[r.kind].id)
                prev_row.watermark = r.watermark  # only short or empty reviews arrived: move past them
        if ess and ess.data:
            session.add(Analysis(game_id=game_id, kind="essence", mode="first" if "essence" not in prev else "update",
                                 provider=provider.name, model=s.llm_model, payload=ess.data,
                                 tokens_in=ess.tokens_in, tokens_out=ess.tokens_out, cost_usd=ess.cost_usd))
        if not any(r.error for r in runs) and not (ess and ess.error):  # a failed part retries on the next check
            game.analyzed_review_count = sum(r.texts_seen for r in runs)
            game.analyzed_at = now
        await session.commit()

    for r in runs:
        await d.bus.emit(f"ai.{r.kind}", "error" if r.error else ("skip" if r.mode == "skip" else "ok"),
                         slug=game.slug, mode=r.mode, model=s.llm_model, read=r.read, texts=r.texts_seen,
                         calls=len(r.calls), tokens_in=r.tokens_in, tokens_out=r.tokens_out,
                         cost_usd=round(r.cost_usd, 5), retried=len(r.calls) > 1 or None, error=r.error)
    if ess:
        await d.bus.emit("ai.essence", "error" if ess.error else "ok", slug=game.slug, tokens_in=ess.tokens_in,
                         tokens_out=ess.tokens_out, cost_usd=round(ess.cost_usd, 5), error=ess.error)


async def update_facets(ctx: dict) -> dict:
    """Light essences for new and changed games, their vectors within today's allowance, then every game's
    similar-games lists. Queued after each discovery run; one at a time."""
    d: Deps = ctx["deps"]
    s = d.settings
    provider = ctx.get("llm")
    if provider is None:
        await d.bus.emit("facets.skipped", "skip", reason="LLM-провайдер не настроен (GA_LLM_PROVIDER)")
        return {}
    async with d.sessions() as session:
        games = await stale_games(session, s.facets_per_run)
    essence = await build_essences(provider, games, s, d.sessions, d.bus) if games else {"ok": 0}
    embed = await embed_pending(provider, s, d.sessions, d.bus, clock.now())
    stats = {"essence": essence, **embed}
    async with d.sessions() as session:
        stats["finished"] = await refinish_all(session)
        empty = await session.scalar(select(SimilarGame.game_id).limit(1)) is None
        if embed["embedded"] or stats["finished"] or empty:
            stats["similar"] = await rebuild(session, s.similar_min_gameplay)
            await d.bus.emit("facets.similar", **stats["similar"])
    await d.bus.emit("facets.done", essences=essence["ok"], embedded=embed["embedded"], waiting=embed["waiting"])
    return stats


async def deep_analyze(ctx: dict, deep_id: int) -> None:
    """Find the most viewed let's play, let Gemini watch three clips of it, store new facts with timecodes."""
    d: Deps = ctx["deps"]
    s = d.settings
    provider = ctx.get("llm")
    async with d.sessions() as session:
        row = await session.get(DeepAnalysis, deep_id)
        if row is None:
            return
        game = await session.get(Game, row.game_id)
        missing = [name for name, ok in (("GA_YOUTUBE_API_KEY", s.youtube_api_key), ("Gemini", provider)) if not ok]
        if missing:
            row.status, row.error = "not_configured", f"Не настроено: {', '.join(missing)}"
        else:
            row.status = "running"
        await session.commit()
    if missing:
        await d.bus.emit("deep.skipped", "skip", slug=game.slug, deep_id=deep_id, reason=row.error)
        return

    async def finish(status: str, error: str | None = None, **values) -> None:
        async with d.sessions() as session:
            r = await session.get(DeepAnalysis, deep_id)
            r.status, r.error = status, error
            for k, v in values.items():
                setattr(r, k, v)
            await session.commit()
        await d.bus.emit("deep.done", "ok" if status == "done" else ("skip" if status in FREE_STATUSES else "error"),
                         slug=game.slug, deep_id=deep_id, result=status, error=error,
                         tokens_in=values.get("tokens_in"), clip_s=values.get("clip_s"))

    queries, qr = await search_queries(provider, game, model=s.llm_model, thinking_level=s.llm_thinking_level)
    q_in, q_out = (qr.tokens_in, qr.tokens_out) if qr else (0, 0)
    yt = YouTube(s.youtube_api_key)
    try:
        await d.bus.emit("deep.search", slug=game.slug, queries=queries)
        videos = await yt.search(queries, s.deep_results_per_query)
    except YouTubeError as e:
        await finish("search_error", str(e), tokens_in=q_in, tokens_out=q_out)
        return
    finally:
        await yt.aclose()
    released = game.release_date is not None and game.release_date <= clock.now().date()
    video = pick_video(game.title, videos, min_views=s.deep_min_views, max_s=s.deep_max_video_s, released=released)
    if video is None:
        await finish("no_video", f"Подходящего летсплея пока нет: просмотрено {len(videos)} видео по {len(queries)} "
                                 "запросам", tokens_in=q_in, tokens_out=q_out)
        return
    clips = pick_clips(video, s.deep_clip_total_s, s.deep_clip_min_s, s.deep_clip_max_s)
    clip_s = sum(c.length for c in clips)
    await d.bus.emit("deep.video", slug=game.slug, video_id=video.id, title=video.title[:120], views=video.views,
                     duration_s=video.duration_s, chapters=len(video.chapters) or None, clips=len(clips), clip_s=clip_s,
                     candidates=len(videos))
    video_fields = {"video_id": video.id, "video_title": video.title[:500], "views": video.views,
                    "duration_s": video.duration_s, "clip_s": clip_s}
    try:
        r = await run_deep(provider, game, video, clips, model=s.llm_model, thinking_level=s.llm_thinking_level,
                           fps=s.deep_fps)
    except Exception as e:
        log.exception("deep analyze failed: %s", game.slug)
        # a call that failed is not billed: the user keeps the slot and can try again
        await finish("failed", f"{type(e).__name__}: {e}"[:300], **video_fields)
        return
    facts = {**r.data, "facts": absolute_facts(r.data, video, clips),
             "video": {"id": video.id, "url": video.url, "title": video.title, "channel": video.channel,
                       "views": video.views, "duration_s": video.duration_s},
             "clips": [{"start_s": c.start_s, "end_s": c.end_s, "label": c.label} for c in clips],
             "model": r.model, "cost_usd": r.cost_usd}
    status, error = ("done", None) if r.data.get("matches_game") else ("mismatch", "Видео не похоже на эту игру")
    await finish(status, error, facts={**facts, "queries": queries}, tokens_in=r.tokens_in + q_in,
                 tokens_out=r.tokens_out + q_out, **video_fields)


_s = get_settings()


class WorkerSettings:
    functions = [
        func(run_hourly, timeout=RUN_TIMEOUT, keep_result=0),
        func(run_bootstrap, timeout=RUN_TIMEOUT, keep_result=0),
        func(run_sweep, timeout=RUN_TIMEOUT, keep_result=0),
        func(analyze_game, keep_result=0),
        func(deep_analyze, keep_result=0),
        func(update_facets, timeout=RUN_TIMEOUT, keep_result=0),
    ]
    cron_jobs = [
        cron(run_hourly, minute=0, timeout=RUN_TIMEOUT, unique=True, job_id="cron:hourly"),
        cron(run_sweep, hour=4, minute=30, timeout=RUN_TIMEOUT, unique=True, job_id="cron:sweep"),
    ] if _s.cron_enabled else []  # the stand runs cycles on command, on its frozen clock
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_s.redis_url)
    max_jobs = 10
