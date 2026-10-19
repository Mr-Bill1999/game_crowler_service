from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or get_settings().database_url, pool_pre_ping=True)


engine = make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create extensions and tables. The advisory lock keeps api and worker from racing on first start."""
    from app import models  # noqa: F401  registers tables on Base.metadata

    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(4242)"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_games_title_trgm ON games USING gin (title gin_trgm_ops)")
        )
        for stmt in MIGRATIONS:
            await conn.execute(text(stmt))


# create_all does not alter existing tables: columns added later are added here, idempotently.
MIGRATIONS = [
    "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS word_count integer",
    r"UPDATE reviews SET word_count = regexp_count(coalesce(quote, ''), '\w+') WHERE word_count IS NULL",
    "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS mode varchar(20) NOT NULL DEFAULT 'full'",
    "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS watermark timestamptz",
    "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS texts_seen integer NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_analyses_game_kind_created ON analyses (game_id, kind, created_at DESC)",
    "ALTER TABLE games ADD COLUMN IF NOT EXISTS top_rank integer",
    "ALTER TABLE games ADD COLUMN IF NOT EXISTS top_seen_at timestamptz",
    "CREATE INDEX IF NOT EXISTS ix_games_top_rank ON games (top_rank)",
    "ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS clip_s integer",
    "ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS requested_by varchar(64)",
    "ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS tokens_in integer NOT NULL DEFAULT 0",
    "ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS tokens_out integer NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_deep_analyses_created ON deep_analyses (created_at)",
    "ALTER TABLE deep_analyses ADD COLUMN IF NOT EXISTS requested_ip varchar(64)",
    "ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS owner varchar(20) NOT NULL DEFAULT 'worker'",
    "CREATE INDEX IF NOT EXISTS ix_analyses_created ON analyses (created_at)",
    "ALTER TABLE game_profiles ADD COLUMN IF NOT EXISTS first_enriched_at timestamptz",
    "UPDATE game_profiles SET first_enriched_at = enriched_at WHERE first_enriched_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_game_profiles_first_enriched_at ON game_profiles (first_enriched_at)",
]
