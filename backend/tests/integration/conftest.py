"""Tests against a real Postgres: GA_TEST_DATABASE_URL, e.g. the stand's postgresql+asyncpg://ga:ga@localhost:5433/ga_test
(stand/stand.sh creates it). Without it they are skipped. Every test starts from empty tables."""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

URL = os.environ.get("GA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GA_TEST_DATABASE_URL not set")


@pytest.fixture
async def sessions():
    if not URL:
        pytest.skip("GA_TEST_DATABASE_URL not set")
    from app import models  # noqa: F401
    from app.core.db import MIGRATIONS, Base

    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        for stmt in MIGRATIONS:
            await conn.execute(text(stmt))
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {names} CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Queue:
    def __init__(self, broken: bool = False):
        self.jobs, self.broken = [], broken

    async def enqueue_job(self, name, *args, **kw):
        if self.broken:
            raise ConnectionError("redis is down")
        self.jobs.append((name, args))
        return object()


@pytest.fixture
async def api(sessions):
    from app.api import main

    async def db():
        async with sessions() as s:
            yield s

    main.app.dependency_overrides[main.db] = db
    main.app.state.arq = Queue()
    async with AsyncClient(transport=ASGITransport(app=main.app, raise_app_exceptions=False),
                           base_url="http://test") as c:
        c.queue = main.app.state.arq
        yield c
    main.app.dependency_overrides.clear()
