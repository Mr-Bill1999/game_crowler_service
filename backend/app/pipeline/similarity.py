"""Similar games by essence, as fixed by the blind experiment (backend/eval/results/similarity-v1/report.md).

score = 0.5 · gameplay + 0.3 · tags + 0.2 · world, each signal z-scored over all other games for that query, so
signals with different spreads weigh what their weights say. Facet vectors are centred on the catalogue mean first:
raw cosines of short Russian texts all sit around 0.8 and say little. Tags are IDF-weighted, rare ones count more,
mechanics more than tone. Metacritic genres are left out: in the experiment they lowered every mix.
Games of the same series go to their own block instead of filling the list.

Kept up to date without recomputing every pair (docs/patches/2026-11-10/similar). The centre and the tag weights are taken
from a snapshot of the catalogue and renewed weekly or after 10 % growth: with them fixed, the similarity of a pair
never changes. Each game keeps running sums of its similarities (its exact mean and spread), a buffer of its best
32 candidates and a bound on everything outside the buffer. A new game costs one pass over the catalogue; a list is
taken from the buffer only when the bound proves it exact, otherwise that row is recomputed.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.light_essence import TAG_GROUP_WEIGHT
from app.models import GameFacets, SimilarGame

WEIGHTS = {"gameplay": 0.5, "tags": 0.3, "world": 0.2}
SIG = ("gameplay", "tags", "world")
W = np.array([WEIGHTS[s] for s in SIG])
TOP_K = 8
REASON_TAGS = 4
BUFFER = 32  # candidates kept per game: in a replay of ten days of prod growth no more than 0.04 % of rows a day fell
# back to a full pass with 32, up to 3 % with 16
BLOCK = 512  # rows per block of a full pass: memory grows with the catalogue, not with its square
MERGE_BLOCK = 2048  # old rows per step when new candidates are merged into their buffers


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
    weight = idf * np.sqrt(np.array([group_weight(t) for t in vocab], dtype=np.float32))
    return unit(x * weight), vocab, weight


def group_weight(tag: str) -> float:
    return TAG_GROUP_WEIGHT.get(tag.split(":", 1)[0], 0.1)


def norm_series(s: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def reasons(a_tags: list[str], b_tags: list[str], zs: dict[str, float], weight: dict[str, float]) -> dict:
    """Why two games are close: shared tags, rarest first, and how far each signal stands above the average pair."""
    shared = sorted(set(a_tags) & set(b_tags), key=lambda t: -weight.get(t, 0))
    return {"tags": shared[:REASON_TAGS], "signals": {s: round(v, 2) for s, v in zs.items()}}


# ---------- snapshot: what the whole catalogue decides, fixed between renewals ----------

@dataclass
class Snapshot:
    centre: dict[str, np.ndarray]  # mean of the unit gameplay and world vectors
    tag_weight: dict[str, float]
    n: int
    min_gameplay: float
    at: datetime | None = None

    def weight(self, tag: str) -> float:
        """A tag the snapshot never saw weighs as one seen once."""
        w = self.tag_weight.get(tag)
        return w if w is not None else float((np.log((1 + self.n) / 2) + 1) * np.sqrt(group_weight(tag)))


def snapshot(items: list[Item], min_gameplay: float, at: datetime | None = None) -> Snapshot:
    _, vocab, weight = tag_matrix([i.tags for i in items])
    return Snapshot({"gameplay": unit(np.stack([i.gameplay for i in items])).mean(axis=0),
                     "world": unit(np.stack([i.world for i in items])).mean(axis=0)},
                    dict(zip(vocab, weight.tolist(), strict=True)), len(items), min_gameplay, at)


# ---------- state: per game sums, buffer, bound ----------

@dataclass
class State:
    snap: Snapshot
    ids: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    keys: list = field(default_factory=list)  # what the row was built from: embed hash, tags, series
    tags: list[list[str]] = field(default_factory=list)
    code: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))  # series, -1 for none
    series_code: dict[str, int] = field(default_factory=dict)
    vocab: dict[str, int] = field(default_factory=dict)
    X: dict[str, np.ndarray] = field(default_factory=dict)  # centred unit gameplay/world, weighted unit tags
    s1: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))  # sum of S over all other games
    s2: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))  # sum of S²
    bid: np.ndarray = field(default_factory=lambda: np.zeros((0, BUFFER), np.int64))  # buffer: game ids, -1 empty
    bS: np.ndarray = field(default_factory=lambda: np.zeros((0, BUFFER, 3), np.float32))  # their raw S
    coef: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))  # weights the bound was taken with
    tref: np.ndarray = field(default_factory=lambda: np.zeros(0))  # best score outside the buffer, at `coef`
    mx: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))  # largest |S| among eligible candidates
    written: dict[int, tuple] = field(default_factory=dict)  # lists as they stand in similar_games

    @property
    def n(self) -> int:
        return len(self.ids)

    def pos(self) -> dict[int, int]:
        return {int(g): i for i, g in enumerate(self.ids)}


def _vectors(st: State, items: list[Item]) -> dict[str, np.ndarray]:
    """Rows for new games in the snapshot's terms; a tag it has not seen gets a column of its own."""
    out = {s: unit(unit(np.stack([i.gameplay if s == "gameplay" else i.world for i in items]).astype(np.float32))
                   - st.snap.centre[s]).astype(np.float32) for s in ("gameplay", "world")}
    for t in sorted({t for i in items for t in i.tags} - st.vocab.keys()):
        st.vocab[t] = len(st.vocab)
        if "tags" in st.X:
            st.X["tags"] = np.hstack([st.X["tags"], np.zeros((st.n, 1), np.float32)])
    w = np.zeros(len(st.vocab), np.float32)
    for t, j in st.vocab.items():
        w[j] = st.snap.weight(t)
    x = np.zeros((len(items), len(st.vocab)), np.float32)
    for r, i in enumerate(items):
        for t in i.tags:
            x[r, st.vocab[t]] = 1
    out["tags"] = unit(x * w).astype(np.float32)
    return out


def _codes(st: State, items: list[Item]) -> np.ndarray:
    out = []
    for i in items:
        name = norm_series(i.series)
        out.append(st.series_code.setdefault(name, len(st.series_code)) if name else -1)
    return np.array(out, np.int64)


def _coef(st: State, rows) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Weight / spread for each signal of each row, and the row's mean and spread: exact, from the running sums."""
    n1 = max(1, st.n - 1)
    mu = st.s1[rows] / n1
    sd = np.sqrt(np.maximum(st.s2[rows] / n1 - mu ** 2, 0))
    sd = np.where(sd <= 1e-9, 1, sd)
    return W / sd, mu, sd


def _signals(st: State, rows: np.ndarray, cols=slice(None)) -> np.ndarray:
    """S of rows × games, signals last."""
    return np.stack([st.X[s][rows] @ st.X[s][cols].T for s in SIG], axis=-1)


def _fill(st: State, rows: np.ndarray, sums: bool, block: int = BLOCK) -> None:
    """A full pass for these rows: S against every game, then sums (when not kept already), buffer and bound."""
    m = min(BUFFER, st.n)
    for i0 in range(0, len(rows), block):
        R = rows[i0:i0 + block]
        ar = np.arange(len(R))
        S = _signals(st, R)
        if sums:
            S64 = S.astype(np.float64)
            self_ = S64[ar, R]
            st.s1[R] = S64.sum(axis=1) - self_
            st.s2[R] = (S64 ** 2).sum(axis=1) - self_ ** 2
        c, _, _ = _coef(st, R)
        r = np.einsum("bns,bs->bn", S, c)
        ok = S[..., 0] >= st.snap.min_gameplay
        ok[ar, R] = False
        code = st.code[R][:, None]
        ok &= ~((code == st.code[None, :]) & (code >= 0))
        r = np.where(ok, r, -np.inf)
        take = min(m + 1, st.n)
        part = np.argpartition(-r, take - 1, axis=1)[:, :take] if st.n > take else np.tile(np.arange(st.n), (len(R), 1))
        vals = np.take_along_axis(r, part, axis=1)
        order = np.argsort(-vals, axis=1, kind="stable")
        part, vals = np.take_along_axis(part, order, axis=1), np.take_along_axis(vals, order, axis=1)
        top, topv = part[:, :m], vals[:, :m]
        valid = np.isfinite(topv)
        bid = np.full((len(R), BUFFER), -1, np.int64)
        bS = np.zeros((len(R), BUFFER, 3), np.float32)
        bid[:, :m] = np.where(valid, st.ids[top], -1)
        bS[:, :m] = np.where(valid[..., None], S[ar[:, None], top], 0)
        st.bid[R], st.bS[R], st.coef[R] = bid, bS, c
        st.tref[R] = vals[:, m] if vals.shape[1] > m else -np.inf
        st.mx[R] = np.where(ok[..., None], np.abs(S), 0).max(axis=1)


def build(items: list[Item], keys: list, snap: Snapshot, block: int = BLOCK) -> State:
    """Every row from scratch, against the given snapshot."""
    st = State(snap)
    st.vocab = {t: j for j, t in enumerate(sorted(snap.tag_weight))}
    _append(st, items, keys)
    _fill(st, np.arange(st.n), sums=True, block=block)
    return st


def _append(st: State, items: list[Item], keys: list) -> np.ndarray:
    vec = _vectors(st, items)
    first = st.n
    for s in SIG:
        st.X[s] = np.vstack([st.X[s], vec[s]]) if s in st.X else vec[s]
    k = len(items)
    st.ids = np.concatenate([st.ids, np.array([i.game_id for i in items], np.int64)])
    st.keys += list(keys)
    st.tags += [list(i.tags) for i in items]
    st.code = np.concatenate([st.code, _codes(st, items)])
    st.s1 = np.vstack([st.s1, np.zeros((k, 3))])
    st.s2 = np.vstack([st.s2, np.zeros((k, 3))])
    st.bid = np.vstack([st.bid, np.full((k, BUFFER), -1, np.int64)])
    st.bS = np.concatenate([st.bS, np.zeros((k, BUFFER, 3), np.float32)])
    st.coef = np.vstack([st.coef, np.zeros((k, 3))])
    st.tref = np.concatenate([st.tref, np.full(k, -np.inf)])
    st.mx = np.vstack([st.mx, np.zeros((k, 3))])
    return np.arange(first, st.n)


def _remove(st: State, gone: set[int]) -> None:
    """Take games out: their similarities leave every other row's sums, their entries leave the buffers."""
    pos = st.pos()
    R = np.array(sorted(pos[g] for g in gone), np.int64)
    keep = np.ones(st.n, bool)
    keep[R] = False
    SR = _signals(st, R, keep).astype(np.float64)
    st.s1[keep] -= SR.sum(axis=0)
    st.s2[keep] -= (SR ** 2).sum(axis=0)
    for s in SIG:
        st.X[s] = st.X[s][keep]
    st.ids, st.code = st.ids[keep], st.code[keep]
    st.keys = [k for k, ok in zip(st.keys, keep, strict=True) if ok]
    st.tags = [t for t, ok in zip(st.tags, keep, strict=True) if ok]
    st.s1, st.s2, st.coef, st.tref, st.mx = st.s1[keep], st.s2[keep], st.coef[keep], st.tref[keep], st.mx[keep]
    st.bid, st.bS = st.bid[keep], st.bS[keep]
    dead = np.isin(st.bid, np.array(sorted(gone), np.int64))
    st.bid[dead] = -1
    st.bS[dead] = 0


def update(st: State, gone: set[int], new: list[Item], new_keys: list, k: int = TOP_K) -> int:
    """Remove and add games, keep every other row exact. Returns how many old rows needed a full pass."""
    if gone:
        _remove(st, gone)
    old = st.n
    A = _append(st, new, new_keys) if new else np.zeros(0, np.int64)
    if len(A):
        SA = _signals(st, A)  # new games × every game
        S_old = SA[:, :old].astype(np.float64)
        st.s1[:old] += S_old.sum(axis=0)
        st.s2[:old] += (S_old ** 2).sum(axis=0)
        _fill(st, A, sums=True)
        elig = SA[:, :old, 0] >= st.snap.min_gameplay
        code = st.code[A][:, None]
        elig &= ~((code == st.code[None, :old]) & (code >= 0))
    fallback = []
    for i0 in range(0, old, MERGE_BLOCK):
        rows = np.arange(i0, min(old, i0 + MERGE_BLOCK))
        c, _, _ = _coef(st, rows)
        ids, S = st.bid[rows], st.bS[rows]
        valid = ids >= 0
        if len(A):
            ids = np.hstack([ids, np.broadcast_to(st.ids[A], (len(rows), len(A)))])
            S = np.concatenate([S, SA[:, rows].transpose(1, 0, 2)], axis=1)
            valid = np.hstack([valid, elig[:, rows].T])
            near = np.where(elig[:, rows].T[..., None], np.abs(SA[:, rows]).transpose(1, 0, 2), 0)
            st.mx[rows] = np.maximum(st.mx[rows], near.max(axis=1))
        r = np.where(valid, np.einsum("bms,bs->bm", S, c), -np.inf)
        order = np.argsort(-r, axis=1, kind="stable")
        ids, S, r, valid = (np.take_along_axis(ids, order, axis=1), np.take_along_axis(S, order[..., None], axis=1),
                            np.take_along_axis(r, order, axis=1), np.take_along_axis(valid, order, axis=1))
        if ids.shape[1] > BUFFER:  # the evicted lift the bound of what is outside
            out = np.where(valid[:, BUFFER:], np.einsum("bms,bs->bm", S[:, BUFFER:], st.coef[rows]), -np.inf)
            st.tref[rows] = np.maximum(st.tref[rows], out.max(axis=1))
        keep = valid[:, :BUFFER]
        st.bid[rows] = np.where(keep, ids[:, :BUFFER], -1)
        st.bS[rows] = np.where(keep[..., None], S[:, :BUFFER], 0)
        # exact when the k-th in the buffer beats anything outside could reach under today's weights
        n_valid = keep.sum(axis=1)
        kth = r[:, k - 1] if r.shape[1] >= k else np.full(len(rows), -np.inf)
        bound = st.tref[rows] + (np.abs(c - st.coef[rows]) * st.mx[rows]).sum(axis=1)
        exact = np.where(n_valid >= k, kth > bound, np.isneginf(st.tref[rows]))
        fallback.extend(rows[~exact].tolist())
    if fallback:
        _fill(st, np.array(fallback, np.int64), sums=False)
    return len(fallback)


# ---------- lists ----------

def lists(st: State, k: int = TOP_K) -> dict[int, tuple[tuple[int, ...], tuple[int, ...]]]:
    """Every game's similar (from its buffer) and same-series lists, best first, as game ids."""
    c, _, _ = _coef(st, slice(None))
    r = np.where(st.bid >= 0, np.einsum("nms,ns->nm", st.bS, c), -np.inf)
    order = np.argsort(-r, axis=1, kind="stable")[:, :k]
    best = np.take_along_axis(st.bid, order, axis=1)
    similar = {int(g): tuple(int(x) for x in row if x >= 0) for g, row in zip(st.ids, best, strict=True)}
    series: dict[int, tuple[int, ...]] = {}
    for code in np.unique(st.code[st.code >= 0]):
        P = np.flatnonzero(st.code == code)
        if len(P) < 2:
            continue
        rs = np.einsum("bns,bs->bn", _signals(st, P, P), c[P])
        np.fill_diagonal(rs, -np.inf)
        for i, a in enumerate(P):
            o = np.argsort(-rs[i], kind="stable")[:min(k, len(P) - 1)]
            series[int(st.ids[a])] = tuple(int(st.ids[P[j]]) for j in o)
    return {g: (similar[g], series.get(g, ())) for g in similar}


def entries(st: State, game_id: int, others: tuple[int, ...], pos: dict[int, int]) -> list[dict]:
    """Rows of similar_games for one list: the fused score and the reasons, under today's mean and spread."""
    if not others:
        return []
    a = pos[game_id]
    B = np.array([pos[o] for o in others])
    S = _signals(st, np.array([a]), B)[0].astype(np.float64)
    _, mu, sd = _coef(st, [a])
    z = (S - mu) / sd
    weight = st.snap.tag_weight
    return [{"other_id": o, "score": round(float(z[j] @ W), 4),
             "reasons": {**reasons(st.tags[a], st.tags[B[j]], {s: float(z[j, i]) for i, s in enumerate(SIG)}, weight),
                         "gameplay": round(float(S[j, 0]), 3)}}
            for j, o in enumerate(others)]


def neighbours(items: list[Item], k: int = TOP_K, min_gameplay: float = -1.0,
               block: int = BLOCK) -> dict[int, dict[str, list[dict]]]:
    """For every game: top-k similar games outside its series, and the games of its series, best first, from a full
    pass with a snapshot of these same items. The fused score is relative to each game's own candidates, so it ranks
    but cannot say "nothing fits": the absolute gameplay similarity does. Below `min_gameplay` a candidate is left
    out of "similar" (blind grades of the experiment: under 0.2 the pair is a miss in 44-93 % of cases and never a
    strong match)."""
    if len(items) < 2:
        return {}
    st = build(items, [None] * len(items), snapshot(items, min_gameplay), block=block)
    pos = st.pos()
    return {g: {"similar": entries(st, g, sim, pos), "series": entries(st, g, ser, pos)}
            for g, (sim, ser) in lists(st, k).items()}


# ---------- the job ----------

VALID = (GameFacets.gameplay_vec.is_not(None), GameFacets.world_vec.is_not(None),
         GameFacets.embed_hash == GameFacets.source_hash)


async def _items(session: AsyncSession, ids: list[int] | None, info: dict) -> list[Item]:
    """Vectors of these games (all valid ones for None), columns only: no essences, no ORM objects."""
    out = []
    chunks = [None] if ids is None else [ids[i:i + 5000] for i in range(0, len(ids), 5000)]
    for chunk in chunks:
        q = select(GameFacets.game_id, GameFacets.gameplay_vec, GameFacets.world_vec).where(*VALID)
        if chunk is not None:
            q = q.where(GameFacets.game_id.in_(chunk))
        for gid, gv, wv in (await session.execute(q)).all():
            tags, series = info[gid]
            out.append(Item(gid, np.asarray(gv, np.float32), np.asarray(wv, np.float32), tags, series))
    out.sort(key=lambda i: i.game_id)
    return out


async def _written(session: AsyncSession) -> dict[int, tuple]:
    rows = (await session.execute(select(SimilarGame.game_id, SimilarGame.kind, SimilarGame.other_id)
                                  .order_by(SimilarGame.game_id, SimilarGame.kind, SimilarGame.rank))).all()
    out: dict[int, dict[str, list[int]]] = {}
    for g, kind, o in rows:
        out.setdefault(g, {"similar": [], "series": []})[kind].append(o)
    return {g: (tuple(v["similar"]), tuple(v["series"])) for g, v in out.items()}


def due_snapshot(st: State | None, n: int, changes: int, min_gameplay: float, now: datetime,
                 growth: float, days: int) -> bool:
    return (st is None or st.snap.min_gameplay != min_gameplay or st.snap.at is None
            or now - st.snap.at >= timedelta(days=days) or n >= round(st.snap.n * (1 + growth)) or changes > n // 4)


async def refresh(session: AsyncSession, cache: dict, now: datetime, min_gameplay: float = -1.0,
                  growth: float = 0.10, days: int = 7) -> dict:
    """Bring similar_games up to date. The state lives in `cache` (the worker's context) between runs; without it,
    after a restart or a failed run, everything is rebuilt from a new snapshot. Only lists that changed are written."""
    st: State | None = cache.get("similar")
    cur = (await session.execute(select(GameFacets.game_id, GameFacets.embed_hash, GameFacets.tags, GameFacets.series)
                                 .where(*VALID))).all()
    info = {g: (list(t or []), s) for g, _, t, s in cur}
    key = {g: (h, tuple(t or []), s) for g, h, t, s in cur}
    try:
        if st is not None:
            known = dict(zip(st.ids.tolist(), st.keys, strict=True))
            gone = {g for g in known if key.get(g) != known[g]}
            add = sorted(g for g in key if known.get(g) != key[g])
        if due_snapshot(st, len(key), len(gone) + len(add) if st is not None else 0, min_gameplay, now, growth, days):
            written = st.written if st is not None else await _written(session)
            items = await _items(session, None, info)
            if len(items) < 2:
                cache.pop("similar", None)
                return {"games": len(items), "mode": "none"}
            st = await asyncio.to_thread(build, items, [key[i.game_id] for i in items],
                                         snapshot(items, min_gameplay, now))
            st.written = written
            mode, fallback, added, removed = "snapshot", 0, len(items), 0
        else:
            new = await _items(session, add, info) if add else []
            fallback = await asyncio.to_thread(update, st, gone, new, [key[i.game_id] for i in new]) \
                if gone or new else 0
            mode, added, removed = "incremental", len(new), len(gone)
        now_lists = await asyncio.to_thread(lists, st)
        changed = [g for g, v in now_lists.items() if st.written.get(g, ((), ())) != v]
        dropped = [g for g in st.written if g not in now_lists]
        pos = st.pos()
        rows = [{"game_id": g, "kind": kind, "rank": rank, **x}
                for g in changed for kind, others in zip(("similar", "series"), now_lists[g], strict=True)
                for rank, x in enumerate(entries(st, g, others, pos))]
        for i in range(0, len(changed) + len(dropped), 2000):
            await session.execute(delete(SimilarGame).where(SimilarGame.game_id.in_((changed + dropped)[i:i + 2000])))
        for i in range(0, len(rows), 2000):
            await session.execute(insert(SimilarGame), rows[i:i + 2000])
        await session.commit()
    except BaseException:
        cache.pop("similar", None)  # the state may be half-updated or ahead of the table: rebuild next time
        raise
    st.written = now_lists
    cache["similar"] = st
    return {"games": st.n, "mode": mode, "added": added, "removed": removed, "recomputed_rows": fallback,
            "lists_written": len(changed), "series_links": sum(len(v[1]) for v in now_lists.values()),
            "few_similar": sum(len(v[0]) < 3 for v in now_lists.values())}
