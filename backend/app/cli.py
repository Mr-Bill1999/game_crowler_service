"""Manual runs.

  python -m app.cli enqueue hourly|bootstrap|sweep [--days N]   queue a run for the worker
  python -m app.cli inline  hourly|bootstrap|sweep [--days N]   run here, without a worker
  python -m app.cli analyze <slug> [<slug> ...]                 analyse these games now, ignoring the trigger
"""

import argparse
import asyncio
import json
import sys

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Game
from app.pipeline import tasks

RUNS = {"hourly": tasks.run_hourly, "bootstrap": tasks.run_bootstrap, "sweep": tasks.run_sweep}


async def analyze(slugs: list[str]) -> None:
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    ctx: dict = {"redis": pool, "cli": True}
    tasks.ctx_owner.set("cli")
    await tasks.startup(ctx)
    try:
        if "llm" not in ctx:
            sys.exit("LLM не настроен: нужны GA_LLM_PROVIDER=gemini и GA_GEMINI_API_KEY в backend/.env")
        async with SessionLocal() as s:
            ids = {g.slug: g.id for g in (await s.execute(select(Game).where(Game.slug.in_(slugs)))).scalars()}
        for slug in slugs:
            if slug not in ids:
                print(f"{slug}: нет в базе")
                continue
            await tasks.analyze_game(ctx, ids[slug])
            print(f"{slug}: готово")
    finally:
        await tasks.shutdown(ctx)
        await pool.aclose()


async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        await analyze(sys.argv[2:])
        return
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["enqueue", "inline"])
    p.add_argument("run", choices=list(RUNS))
    p.add_argument("--days", type=int, default=None)
    a = p.parse_args()
    args = [a.days] if a.run == "bootstrap" and a.days else []

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        if a.mode == "enqueue":
            job = await pool.enqueue_job(RUNS[a.run].__name__, *args, _job_id=f"manual:{a.run}")
            print("queued" if job else "already queued or running")
            return
        ctx: dict = {"redis": pool, "cli": True}
        tasks.ctx_owner.set("cli")
        await tasks.startup(ctx)
        try:
            print(json.dumps(await RUNS[a.run](ctx, *args), ensure_ascii=False, indent=2))
        finally:
            await tasks.shutdown(ctx)
    finally:
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
