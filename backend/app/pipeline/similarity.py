"""Similar games by essence, as fixed by the blind experiment (backend/eval/results/similarity-v1/report.md).

score = 0.5 · gameplay + 0.3 · tags + 0.2 · world, each signal z-scored over all other games for that query, so
signals with different spreads weigh what their weights say. Facet vectors are centred on the catalogue mean first:
raw cosines of short Russian texts all sit around 0.8 and say little. Tags are IDF-weighted, rare ones count more,
mechanics more than tone. Metacritic genres are left out: in the experiment they lowered every mix.
Games of the same series go to their own block instead of filling the list.
"""

import asyncio
import re
from dataclasses import dataclass

import numpy as np
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.light_essence import TAG_GROUP_WEIGHT
from app.models import GameFacets, SimilarGame

WEIGHTS = {"gameplay": 0.5, "tags": 0.3, "world": 0.2}
TOP_K = 8
REASON_TAGS = 4


@dataclass
class Item:
    game_id: int
    gameplay: np.ndarray
    world: np.ndarray
    tags: list[str]
    series: str | None = None


def unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


def centred(m: np.ndarray) -> np.ndarray:
    m = unit(m)
    return unit(m - m.mean(axis=0))


def tag_matrix(tag_lists: list[list[str]]) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Rows are unit vectors of IDF × group weight; returns them, the vocabulary seen and each tag's weight."""
    vocab = sorted({t for tags in tag_lists for t in tags})
    idx = {t: i for i, t in enumerate(vocab)}
    x = np.zeros((len(tag_lists), len(vocab)), dtype=np.float32)
    for i, tags in enumerate(tag_lists):
        for t in tags:
            x[i, idx[t]] = 1
    idf = np.log((1 + len(tag_lists)) / (1 + x.sum(axis=0))) + 1
    group = np.array([TAG_GROUP_WEIGHT.get(t.split(":", 1)[0], 0.1) for t in vocab], dtype=np.float32)
    weight = idf * np.sqrt(group)
    return unit(x * weight), vocab, weight


def zscore_rows(m: np.ndarray, first: int = 0) -> np.ndarray:
    """Each row z-scored over all other games: row i of the block is game `first + i`, whose own cell is left out."""
    m = m.astype(np.float64)
    r = np.arange(m.shape[0])
    m[r, first + r] = np.nan
    mu, sd = np.nanmean(m, axis=1, keepdims=True), np.nanstd(m, axis=1, keepdims=True)
    return np.nan_to_num((m - mu) / np.where(sd == 0, 1, sd), nan=0.0)


def norm_series(s: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


BLOCK = 512  # rows per block: ~5 matrices of BLOCK x N doubles at a time instead of 7 of N x N


def neighbours(items: list[Item], k: int = TOP_K, min_gameplay: float = -1.0,
               block: int = BLOCK) -> dict[int, dict[str, list[dict]]]:
    """For every game: top-k similar games outside its series, and the games of its series, best first.
    The fused score is relative to each game's own candidates, so it ranks but cannot say "nothing fits": the
    absolute gameplay similarity does. Below `min_gameplay` a candidate is left out of "similar" (blind grades of
    the experiment: under 0.2 the pair is a miss in 44-93 % of cases and never a strong match).

    Computed a block of games at a time, with a partial sort for the top k: memory grows with the catalogue, not
    with its square (3 625 games: 0.9 -> 0.44 GB; 7 000: 2.7 -> 0.66 GB). The lists are the same as the full-matrix
    version's, which tests/test_similarity.py keeps as the reference."""
    n = len(items)
    if n < 2:
        return {}
    gameplay = centred(np.stack([i.gameplay for i in items]))
    world = centred(np.stack([i.world for i in items]))
    tags, vocab, tag_weight = tag_matrix([i.tags for i in items])
    idf_weight = dict(zip(vocab, tag_weight.tolist(), strict=True))
    series = [norm_series(i.series) for i in items]
    members: dict[str, np.ndarray] = {}
    for j, name in enumerate(series):
        if name:
            members.setdefault(name, []).append(j)
    members = {name: np.array(v) for name, v in members.items()}

    out: dict[int, dict[str, list[dict]]] = {}
    for first in range(0, n, block):
        last = min(n, first + block)
        signals = {"gameplay": gameplay[first:last] @ gameplay.T, "world": world[first:last] @ world.T,
                   "tags": tags[first:last] @ tags.T}
        z = {name: zscore_rows(m, first) for name, m in signals.items()}
        fusion = sum(w * z[name] for name, w in WEIGHTS.items())
        rows = np.arange(last - first)
        fusion[rows, first + rows] = -np.inf
        for i in rows:
            a = first + int(i)
            f = fusion[i]
            same = members.get(series[a]) if series[a] else None
            same = same[same != a] if same is not None else np.array([], dtype=int)
            ok = signals["gameplay"][i] >= min_gameplay
            ok[a] = False
            ok[same] = False
            cand = np.flatnonzero(ok)
            if len(cand) > k:
                cand = cand[np.argpartition(-f[cand], k)[:k]]
            lists = {"similar": cand[np.argsort(-f[cand], kind="stable")],
                     "series": same[np.argsort(-f[same], kind="stable")][:k]}
            out[items[a].game_id] = {
                kind: [{"other_id": items[b].game_id, "score": round(float(f[b]), 4),
                        "reasons": {**reasons(items[a], items[b], {s: float(z[s][i, b]) for s in WEIGHTS},
                                              idf_weight),
                                    "gameplay": round(float(signals["gameplay"][i, b]), 3)}}
                       for b in lst]
                for kind, lst in lists.items()}
    return out


def reasons(a: Item, b: Item, zs: dict[str, float], weight: dict[str, float]) -> dict:
    """Why two games are close: shared tags, rarest first, and how far each signal stands above the average pair."""
    shared = sorted(set(a.tags) & set(b.tags), key=lambda t: -weight.get(t, 0))
    return {"tags": shared[:REASON_TAGS], "signals": {s: round(v, 2) for s, v in zs.items()}}


async def rebuild(session: AsyncSession, min_gameplay: float = -1.0) -> dict:
    """Recompute every game's lists from the facets that have up-to-date vectors."""
    rows = (await session.execute(select(GameFacets).where(
        GameFacets.gameplay_vec.is_not(None), GameFacets.world_vec.is_not(None),
        GameFacets.embed_hash == GameFacets.source_hash))).scalars().all()
    items = [Item(r.game_id, np.asarray(r.gameplay_vec, dtype=np.float32), np.asarray(r.world_vec, dtype=np.float32),
                  list(r.tags or []), r.series) for r in rows]
    # CPU-bound for seconds: in a thread, so the worker keeps serving its other jobs and its heartbeat meanwhile
    lists = await asyncio.to_thread(neighbours, items, min_gameplay=min_gameplay)
    rows = [{"game_id": game_id, "kind": kind, "rank": rank, **x}
            for game_id, kinds in lists.items() for kind, lst in kinds.items() for rank, x in enumerate(lst)]
    await session.execute(delete(SimilarGame))
    for i in range(0, len(rows), 2000):  # one statement per 2 000 rows instead of one ORM object per row
        await session.execute(insert(SimilarGame), rows[i:i + 2000])
    await session.commit()
    return {"games": len(items), "series_links": sum(len(v["series"]) for v in lists.values()),
            "few_similar": sum(len(v["similar"]) < 3 for v in lists.values())}
