"""similar_games kept up to date against a snapshot: a full pass once, then only new and changed games."""

from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select, update

from app.models import FACET_DIM, Game, GameFacets, SimilarGame
from app.pipeline.similarity import Item, build, lists, refresh

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
TAGS = [f"mechanics:{i}" for i in range(12)] + [f"tone:{i}" for i in range(6)]


def facets(i: int, rng) -> GameFacets:
    centre = np.eye(FACET_DIM, dtype=np.float32)[i % 5] * 3
    return GameFacets(game_id=i, essence={}, tags=list(rng.choice(TAGS, 4, replace=False)),
                      series="Saga" if i % 7 == 0 else None, source_hash=f"h{i}", embed_hash=f"h{i}", model="m",
                      gameplay_vec=(centre + rng.standard_normal(FACET_DIM)).tolist(),
                      world_vec=rng.standard_normal(FACET_DIM).tolist(), embedded_at=NOW)


async def add(sessions, ids, seed):
    rng = np.random.default_rng(seed)
    async with sessions() as s:
        s.add_all([Game(id=i, slug=f"g{i}", title=f"G{i}", discovered_at=NOW) for i in ids])
        await s.flush()
        s.add_all([facets(i, rng) for i in ids])
        await s.commit()


async def table(sessions) -> dict[int, tuple]:
    async with sessions() as s:
        rows = (await s.execute(select(SimilarGame.game_id, SimilarGame.kind, SimilarGame.other_id)
                                .order_by(SimilarGame.game_id, SimilarGame.kind, SimilarGame.rank))).all()
    out: dict[int, dict] = {}
    for g, kind, o in rows:
        out.setdefault(g, {"similar": [], "series": []})[kind].append(o)
    return {g: (tuple(v["similar"]), tuple(v["series"])) for g, v in out.items()}


async def expected(sessions, snap) -> dict[int, tuple]:
    async with sessions() as s:
        rows = (await s.execute(select(GameFacets).where(GameFacets.embed_hash == GameFacets.source_hash))).scalars().all()
    items = sorted((Item(r.game_id, np.asarray(r.gameplay_vec, np.float32), np.asarray(r.world_vec, np.float32),
                         list(r.tags), r.series) for r in rows), key=lambda i: i.game_id)
    return {g: v for g, v in lists(build(items, [None] * len(items), snap)).items() if v != ((), ())}


async def test_first_pass_then_only_new_and_changed_games(sessions):
    await add(sessions, range(1, 121), seed=1)
    cache: dict = {}
    async with sessions() as s:
        first = await refresh(s, cache, NOW, 0.0)
    assert first["mode"] == "snapshot" and first["lists_written"] == 120
    snap = cache["similar"].snap
    assert await table(sessions) == await expected(sessions, snap)

    await add(sessions, range(121, 126), seed=2)  # five new games
    async with sessions() as s:  # one game's essence changed and waits for new vectors: it leaves every list
        await s.execute(update(GameFacets).where(GameFacets.game_id == 3).values(source_hash="new"))
        await s.commit()
    async with sessions() as s:
        second = await refresh(s, cache, NOW + timedelta(hours=1), 0.0)
    assert second["mode"] == "incremental" and second["added"] == 5 and second["removed"] == 1
    assert 5 <= second["lists_written"] < 120  # the new games and the lists they entered or game 3 left
    got = await table(sessions)
    assert got == await expected(sessions, snap)
    assert 3 not in got and all(3 not in v[0] for v in got.values())


async def test_a_restart_with_nothing_new_rewrites_nothing(sessions):
    await add(sessions, range(1, 61), seed=3)
    async with sessions() as s:
        await refresh(s, {}, NOW, 0.0)
        stamp = await s.scalar(select(func.max(SimilarGame.computed_at)))
    async with sessions() as s:  # a new worker: no state, a new snapshot of the same catalogue
        again = await refresh(s, {}, NOW + timedelta(hours=1), 0.0)
        assert again["mode"] == "snapshot" and again["lists_written"] == 0
        assert await s.scalar(select(func.max(SimilarGame.computed_at))) == stamp


async def test_a_failed_run_drops_the_state(sessions, monkeypatch):
    from app.pipeline import similarity

    await add(sessions, range(1, 41), seed=4)
    cache: dict = {}
    async with sessions() as s:
        await refresh(s, cache, NOW, 0.0)
    await add(sessions, [41], seed=5)

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(similarity, "update", broken)
    async with sessions() as s:
        try:
            await refresh(s, cache, NOW, 0.0)
        except RuntimeError:
            pass
    assert "similar" not in cache  # the next run starts from a new snapshot and the table, not from a stale state
