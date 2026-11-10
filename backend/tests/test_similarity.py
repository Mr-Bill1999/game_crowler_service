import numpy as np

from app.ai.light_essence import clean_tags, flat_tags, source_hash
from app.models import Game
from app.pipeline.similarity import Item, neighbours, tag_matrix


def vec(*hot: int, dim: int = 8) -> np.ndarray:
    v = np.full(dim, 0.05, dtype=np.float32)
    for h in hot:
        v[h] = 1
    return v


def catalogue() -> list[Item]:
    # query 1 plays like 2 (gameplay), lives in the world of 3, shares rare tags with 4; 5..8 are unrelated
    items = [
        Item(1, vec(0), vec(0), ["mechanics:stealth", "mechanics:loot", "tone:dark"]),
        Item(2, vec(0), vec(5), ["tone:cozy"]),
        Item(3, vec(3), vec(0), ["tone:cozy"]),
        Item(4, vec(4), vec(6), ["mechanics:stealth", "mechanics:loot"]),
    ]
    # fillers never touch dimension 0, where the query's gameplay and world live
    items += [Item(i, vec(1 + i % 7, 1 + (i + 3) % 7), vec(1 + (i + 1) % 7, 1 + (i + 5) % 7), ["tone:cozy"])
              for i in range(5, 12)]
    return items


def test_gameplay_outweighs_tags_outweighs_world():
    top = [x["other_id"] for x in neighbours(catalogue())[1]["similar"][:3]]
    assert top == [2, 4, 3]


def test_reasons_list_shared_tags_rarest_first():
    first = {x["other_id"]: x for x in neighbours(catalogue())[1]["similar"]}
    assert set(first[4]["reasons"]["tags"]) == {"mechanics:stealth", "mechanics:loot"}
    assert first[2]["reasons"]["signals"]["gameplay"] > 1


def test_same_series_goes_to_its_own_block():
    items = catalogue()
    items[0].series, items[1].series = "Thief", "thief!"
    out = neighbours(items)[1]
    assert [x["other_id"] for x in out["series"]] == [2]
    assert 2 not in [x["other_id"] for x in out["similar"]]


def test_rare_tags_weigh_more():
    _, vocab, weight = tag_matrix([["tone:cozy", "mechanics:stealth"]] + [["tone:cozy"]] * 9)
    w = dict(zip(vocab, weight, strict=True))
    assert w["mechanics:stealth"] > w["tone:cozy"]


def test_tags_are_cleaned_and_flattened():
    e = clean_tags({"mechanics": ["stealth", "stealth", "made-up", "loot"], "perspective": ["top-down", "isometric"],
                    "modes": [], "setting": None, "tone": ["dark"]})
    assert e["mechanics"] == ["stealth", "loot"] and e["perspective"] == ["top-down"] and e["setting"] == []
    assert flat_tags(e) == ["mechanics:stealth", "mechanics:loot", "perspective:top-down", "tone:dark"]


def test_essence_is_rebuilt_only_when_its_sources_change():
    g = Game(title="X", genres=["Action"], description="Fight  things.")
    same = Game(title="X", genres=["Action"], description="Fight things. ")
    assert source_hash(g) == source_hash(same)
    assert source_hash(g) != source_hash(Game(title="X", genres=["Action"], description="Build things."))


def test_floor_leaves_out_games_that_do_not_play_alike():
    items = catalogue()
    loose = [x["other_id"] for x in neighbours(items)[1]["similar"]]
    strict = [x["other_id"] for x in neighbours(items, min_gameplay=0.5)[1]["similar"]]
    assert len(strict) < len(loose) and strict[0] == 2  # the one that plays alike stays
    assert all(x["reasons"]["gameplay"] >= 0.5 for x in neighbours(items, min_gameplay=0.5)[1]["similar"])


# ---------- block computation = the full-matrix original ----------

def reference_neighbours(items, k=8, min_gameplay=-1.0):
    """The full N x N version this module used until November 2026, kept as the reference for the block one."""
    from app.pipeline.similarity import WEIGHTS, centred, norm_series, reasons

    def zscore(m):
        m = m.astype(np.float64)
        np.fill_diagonal(m, np.nan)
        mu, sd = np.nanmean(m, axis=1, keepdims=True), np.nanstd(m, axis=1, keepdims=True)
        return np.nan_to_num((m - mu) / np.where(sd == 0, 1, sd), nan=0.0)

    gameplay = centred(np.stack([i.gameplay for i in items]))
    world = centred(np.stack([i.world for i in items]))
    tags, vocab, tag_weight = tag_matrix([i.tags for i in items])
    signals = {"gameplay": gameplay @ gameplay.T, "world": world @ world.T, "tags": tags @ tags.T}
    z = {name: zscore(m) for name, m in signals.items()}
    fusion = sum(w * z[name] for name, w in WEIGHTS.items())
    np.fill_diagonal(fusion, -np.inf)
    idf = dict(zip(vocab, tag_weight.tolist(), strict=True))
    series = [norm_series(i.series) for i in items]
    out = {}
    for a in range(len(items)):
        same, other = [], []
        for b in np.argsort(-fusion[a]):
            if b == a:
                continue
            if series[a] and series[b] == series[a]:
                same.append(int(b))
            elif signals["gameplay"][a, b] >= min_gameplay:
                other.append(int(b))
        out[items[a].game_id] = {
            kind: [{"other_id": items[b].game_id, "score": round(float(fusion[a, b]), 4),
                    "reasons": {**reasons(items[a].tags, items[b].tags, {s: float(z[s][a, b]) for s in WEIGHTS}, idf),
                                "gameplay": round(float(signals["gameplay"][a, b]), 3)}}
                   for b in lst[:k]]
            for kind, lst in (("similar", other), ("series", same))}
    return out


def clustered(n: int, dim: int = 32, seed: int = 3) -> list[Item]:
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((6, dim)).astype(np.float32)
    tags = [f"mechanics:{i}" for i in range(20)] + [f"tone:{i}" for i in range(10)]
    return [Item(i, centres[i % 6] + 0.8 * rng.standard_normal(dim).astype(np.float32),
                 centres[(i + 1) % 6] + rng.standard_normal(dim).astype(np.float32),
                 list(rng.choice(tags, 5, replace=False)), f"Series {i % 9}" if i % 3 == 0 else None)
            for i in range(n)]


def test_block_version_gives_the_same_lists_as_the_full_matrix():
    items = clustered(300)
    for min_gameplay in (-1.0, 0.2):
        want = reference_neighbours(items, min_gameplay=min_gameplay)
        got = neighbours(items, min_gameplay=min_gameplay, block=64)  # several blocks, the last one short
        assert got.keys() == want.keys()
        for g in want:
            for kind in ("similar", "series"):
                w = [(x["other_id"], x["score"]) for x in want[g][kind]]
                h = [(x["other_id"], x["score"]) for x in got[g][kind]]
                # scores agree to the last rounding step: mean and spread now come from running sums
                assert np.allclose(sorted(s for _, s in w), sorted(s for _, s in h), atol=1.5e-4), (g, kind)
                assert {i for i, _ in w} == {i for i, _ in h} or len({s for _, s in w}) < len(w), (g, kind)
                # reasons: the same tags; signals equal up to the last rounding step (float32 sums in blocks
                # can land on the other side of a .xx5)
                rw = {x["other_id"]: x["reasons"] for x in want[g][kind]}
                for x in got[g][kind]:
                    if x["other_id"] in rw:
                        r = rw[x["other_id"]]
                        assert x["reasons"]["tags"] == r["tags"] and x["reasons"]["gameplay"] == r["gameplay"]
                        assert all(abs(x["reasons"]["signals"][s] - r["signals"][s]) <= 0.011 for s in r["signals"])


# ---------- incremental: the same lists as a full pass with the same snapshot ----------

def test_incremental_gives_the_lists_of_a_full_pass_with_the_same_snapshot():
    from dataclasses import replace

    from app.pipeline.similarity import build, lists, snapshot, update

    pool = clustered(520, seed=5)
    live = {i.game_id: i for i in pool[:300]}
    snap = snapshot(list(live.values()), 0.0)
    st = build(sorted(live.values(), key=lambda i: i.game_id), [None] * len(live), snap, block=64)
    rng = np.random.default_rng(7)
    later = iter(pool[300:])
    for step in range(8):
        gone = {int(g) for g in rng.choice(sorted(live), 6, replace=False)}
        new = [next(later) for _ in range(25)]
        # a game whose essence changed comes back with other tags and a series: removed and added again
        moved = sorted(gone)[:2]
        new += [replace(live[g], tags=live[g].tags[:2] + [f"setting:new{step}"], series="Series 1") for g in moved]
        for g in gone:
            live.pop(g)
        live.update({i.game_id: i for i in new})
        update(st, gone, new, [None] * len(new))
        want = lists(build(sorted(live.values(), key=lambda i: i.game_id), [None] * len(live), snap))
        assert lists(st) == want, step


def test_lists_stay_exact_when_buffers_run_dry():
    from app.pipeline.similarity import build, lists, snapshot, update

    items = clustered(200, seed=11)
    snap = snapshot(items, -1.0)
    st = build(items, [None] * len(items), snap)
    live = {i.game_id: i for i in items}
    for row in (0, 1):  # a game's whole buffer leaves: its row must fall back to a full pass and stay exact
        gone = {int(g) for g in st.bid[row] if g >= 0}
        for g in gone:
            live.pop(g)
        recomputed = update(st, gone, [], [])
        assert recomputed > 0
        assert lists(st) == lists(build(sorted(live.values(), key=lambda i: i.game_id), [None] * len(live), snap))


def test_snapshot_is_renewed_weekly_after_growth_or_on_a_new_floor():
    from datetime import UTC, datetime, timedelta

    from app.pipeline.similarity import State, due_snapshot, snapshot

    t = datetime(2026, 11, 1, tzinfo=UTC)
    st = State(snapshot(clustered(100), 0.2, t))
    assert due_snapshot(None, 100, 0, 0.2, t, 0.1, 7)
    assert not due_snapshot(st, 109, 5, 0.2, t + timedelta(days=6), 0.1, 7)
    assert due_snapshot(st, 110, 5, 0.2, t, 0.1, 7)  # +10 %
    assert due_snapshot(st, 100, 5, 0.2, t + timedelta(days=7), 0.1, 7)
    assert due_snapshot(st, 100, 5, 0.3, t, 0.1, 7)  # the floor changed
    assert due_snapshot(st, 100, 26, 0.2, t, 0.1, 7)  # a quarter of the catalogue changed: a full pass is cheaper
