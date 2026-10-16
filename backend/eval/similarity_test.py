"""Similar games by essence: four ways to pick the top 5, judged blind on a shared pool.

  V0 genre      Metacritic's own carousel: same genre, sorted by Metascore
  V1 whole      one embedding of the whole essence text, names included, raw cosine
  V2 gameplay   embedding of the gameplay facet only, centred on the corpus mean
  V3 fusion     gameplay 0.40 + tags 0.25 + world 0.20 + Metacritic genre 0.15, each signal z-scored per query;
                same-series games go to their own block and are not in the list

The essence is the light one: built from the store description and genres alone, so every game has it,
not only the ones with enough reviews. Facet texts carry no proper names; names live in series/comparable_to.

  cd backend && .venv/bin/python -m eval.similarity_test essence  [--out DIR]   ~25 min at 12 RPM, resumable
  .venv/bin/python -m eval.similarity_test embed    --out DIR                    ~20 min, free tier 100 texts/min
  .venv/bin/python -m eval.similarity_test pool     --out DIR [--queries 20]     rankings, metrics, blind packet
  .venv/bin/python -m eval.similarity_test finalize --out DIR                    reads DIR/grades.json
"""

import argparse
import asyncio
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types
from sqlalchemy import func, select

from app.ai.gemini import GeminiProvider
from app.ai.light_essence import (
    LIGHT_ESSENCE_SCHEMA,
    LIGHT_ESSENCE_SYSTEM,
    MIN_DESCRIPTION,
    TAG_GROUP_WEIGHT,
    TAG_GROUPS,
    essence_prompt,
    texts,
)
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Game

MODEL, THINKING = "gemini-3.5-flash-lite", "minimal"
# Free tier per embedding model: 100 texts a minute and 1000 a day, every text in a batch counts.
EMBED_MODEL, EMBED_DIM, EMBED_BATCH = "gemini-embedding-2", 768, 45
TOP_K = 5
WEIGHTS = {"gameplay": 0.40, "tags": 0.25, "world": 0.20, "genre": 0.15}
VARIANTS = ["V0", "V1", "V2", "V3"]
RESULTS = Path(__file__).parent / "results"



async def load_games() -> list[Game]:
    async with SessionLocal() as s:
        q = select(Game).where(func.length(func.coalesce(Game.description, "")) >= MIN_DESCRIPTION)
        return list((await s.scalars(q.order_by(Game.id))).all())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# ---------- stage 1: light essence ----------

async def stage_essence(out: Path, limit: int | None = None) -> None:
    s = get_settings()
    llm = GeminiProvider(s.gemini_api_key, MODEL, rpm=s.llm_rpm, concurrency=s.llm_concurrency)
    path = out / "essence.jsonl"
    done = {r["id"] for r in read_jsonl(path)}
    games = [g for g in await load_games() if g.id not in done][:limit]
    print(f"essence: {len(done)} done, {len(games)} to go")
    lock, stats = asyncio.Lock(), Counter()

    async def one(g: Game) -> None:
        try:
            r = await llm.generate_json(system=LIGHT_ESSENCE_SYSTEM, prompt=essence_prompt(g),
                                        schema=LIGHT_ESSENCE_SCHEMA, thinking_level=THINKING, max_output_tokens=2048)
        except Exception as e:  # noqa: BLE001 - one game failing must not stop the run
            stats["error"] += 1
            print(f"  ! {g.slug}: {str(e)[:120]}")
            return
        e = r.data
        for k, (allowed, _, hi) in TAG_GROUPS.items():  # belt and braces: the schema enum is advisory on some paths
            e[k] = [t for t in dict.fromkeys(e.get(k, [])) if t in allowed][:hi]
        row = {"id": g.id, "slug": g.slug, "title": g.title, "genres": g.genres, "metascore": g.metascore,
               "description": " ".join((g.description or "").split()), "essence": e,
               "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cost_usd": r.cost_usd}
        async with lock:
            with path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats["ok"] += 1
            stats["tokens_in"] += r.tokens_in
            stats["tokens_out"] += r.tokens_out
            if stats["ok"] % 25 == 0:
                print(f"  {stats['ok']}/{len(games)}  in={stats['tokens_in']} out={stats['tokens_out']}")

    await asyncio.gather(*(one(g) for g in games))
    print(f"essence: {dict(stats)}")


# ---------- stage 2: embeddings ----------

async def stage_embed(out: Path, model: str = EMBED_MODEL) -> None:
    rows = read_jsonl(out / "essence.jsonl")
    facets = ["whole", "gameplay", "world"]
    path = out / "embeddings.npz"
    have = dict(np.load(path)) if path.exists() else {}
    if have.get("ids") is not None and list(have["ids"]) == [r["id"] for r in rows]:
        print("embeddings are up to date")
        return
    items = [texts(r["essence"], r["title"])[f] for r in rows for f in facets]
    client = genai.Client(api_key=get_settings().gemini_api_key)
    partial = out / f"embeddings.{model}.partial.jsonl"  # one line per batch, so a stopped run resumes where it was
    vecs: list[list[float]] = [v for b in read_jsonl(partial) for v in b["vectors"]]
    if vecs:
        print(f"  resuming at {len(vecs)}/{len(items)}")
    for i in range(len(vecs), len(items), EMBED_BATCH):
        # gemini-embedding-2 merges a list of strings into one vector; each text has to be its own Content
        r = await client.aio.models.embed_content(
            model=model, contents=[types.Content(parts=[types.Part(text=t)]) for t in items[i:i + EMBED_BATCH]],
            config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=EMBED_DIM))
        batch = [e.values for e in r.embeddings]
        vecs += batch
        with partial.open("a") as f:
            f.write(json.dumps({"start": i, "vectors": batch}) + "\n")
        print(f"  embedded {len(vecs)}/{len(items)}", flush=True)
        if len(vecs) < len(items):
            await asyncio.sleep(62)  # free tier: 100 texts per minute per model, every text in a batch counts
    m = np.array(vecs, dtype=np.float32).reshape(len(rows), len(facets), EMBED_DIM)
    np.savez(path, ids=np.array([r["id"] for r in rows]), model=np.array(model), **{f: m[:, k] for k, f in enumerate(facets)})


# ---------- stage 3: rankings ----------

def unit(m: np.ndarray) -> np.ndarray:
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def centred(m: np.ndarray) -> np.ndarray:
    m = unit(m)
    return unit(m - m.mean(axis=0))


def tag_matrix(rows: list[dict]) -> np.ndarray:
    """IDF-weighted tag vectors, groups weighted by how much they say about play; cosine of these is the tag score."""
    vocab = [(k, t) for k, (v, _, _) in TAG_GROUPS.items() for t in v]
    idx = {kt: i for i, kt in enumerate(vocab)}
    x = np.zeros((len(rows), len(vocab)), dtype=np.float32)
    for i, r in enumerate(rows):
        for k in TAG_GROUPS:
            for t in r["essence"][k]:
                x[i, idx[(k, t)]] = 1
    df = x.sum(axis=0)
    idf = np.log((1 + len(rows)) / (1 + df)) + 1
    gw = np.array([TAG_GROUP_WEIGHT[k] for k, _ in vocab], dtype=np.float32)
    x = x * idf * np.sqrt(gw)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(n == 0, 1, n)


def genre_overlap(rows: list[dict]) -> np.ndarray:
    sets = [set(r["genres"]) for r in rows]
    n = len(rows)
    out = np.zeros((n, n), dtype=np.float32)
    for i in range(n):
        for j in range(n):
            if sets[i] and sets[j]:
                out[i, j] = len(sets[i] & sets[j]) / len(sets[i] | sets[j])
    return out


def norm_series(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def zscore_rows(m: np.ndarray) -> np.ndarray:
    """Per query, over all other games: puts signals with different spreads on one scale before weighting."""
    m = m.copy()
    np.fill_diagonal(m, np.nan)
    mu, sd = np.nanmean(m, axis=1, keepdims=True), np.nanstd(m, axis=1, keepdims=True)
    return (m - mu) / np.where(sd == 0, 1, sd)


def rankings(rows: list[dict], emb: dict) -> tuple[dict, dict]:
    n = len(rows)
    whole, gameplay, world = unit(emb["whole"]), centred(emb["gameplay"]), centred(emb["world"])
    sims = {
        "whole": whole @ whole.T,
        "gameplay": gameplay @ gameplay.T,
        "world": world @ world.T,
        "tags": (t := tag_matrix(rows)) @ t.T,
        "genre": genre_overlap(rows),
    }
    fusion = sum(w * np.nan_to_num(zscore_rows(sims[k]), nan=0.0) for k, w in WEIGHTS.items())
    series = [norm_series(r["essence"]["series"]) for r in rows]
    meta = [r["metascore"] if r["metascore"] is not None else -1 for r in rows]

    def top(scores: np.ndarray, i: int, skip=lambda j: False) -> list[int]:
        order = [j for j in np.argsort(-scores) if j != i and not skip(j)]
        return order[:TOP_K]

    out: dict[str, list[list[int]]] = {v: [] for v in VARIANTS}
    series_block: list[list[int]] = []
    for i in range(n):
        g = sims["genre"][i]
        # Metacritic: any shared genre, best Metascore first; ties broken by overlap, then title for stability
        key = np.array([(g[j] > 0) * 1e6 + meta[j] * 10 + g[j] for j in range(n)])
        out["V0"].append(top(key, i, lambda j, g=g: g[j] == 0))
        out["V1"].append(top(sims["whole"][i], i))
        out["V2"].append(top(sims["gameplay"][i], i))
        same = (lambda j, s=series[i]: bool(s) and series[j] == s)
        out["V3"].append(top(fusion[i], i, same))
        series_block.append([j for j in range(n) if j != i and same(j)])
    return out, {"sims": sims, "fusion": fusion, "series": series_block}


def hubness(lists: list[list[int]], n: int) -> dict:
    occ = Counter(j for lst in lists for j in lst)
    k = np.array([occ.get(j, 0) for j in range(n)], dtype=float)
    skew = float(((k - k.mean()) ** 3).mean() / (k.std() ** 3)) if k.std() else 0.0
    return {"max_k_occurrence": int(k.max()), "skew": round(skew, 2),
            "never_recommended": round(float((k == 0).mean()), 3),
            "top_hubs": [(int(j), int(c)) for j, c in occ.most_common(3)]}


def pick_queries(rows: list[dict], n: int, seed: int = 7) -> list[int]:
    """One query per main genre, round-robin over genres, only games whose description is enough to judge by."""
    rng = random.Random(seed)
    by_genre: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["essence"]["confidence"] != "low" and r["genres"]:
            by_genre[r["genres"][0]].append(i)
    for v in by_genre.values():
        rng.shuffle(v)
    genres = sorted(by_genre, key=lambda k: -len(by_genre[k]))
    picked: list[int] = []
    while len(picked) < n and any(by_genre.values()):
        for gname in genres:
            if by_genre[gname] and len(picked) < n:
                picked.append(by_genre[gname].pop())
    return picked


def card(r: dict, chars: int = 420) -> dict:
    d = r["description"]
    return {"title": r["title"], "genres": r["genres"], "description": d[:chars] + ("…" if len(d) > chars else "")}


def stage_pool(out: Path, n_queries: int) -> None:
    rows = read_jsonl(out / "essence.jsonl")
    rows.sort(key=lambda r: r["id"])
    npz = np.load(out / "embeddings.npz")
    order = {int(i): k for k, i in enumerate(npz["ids"])}
    emb = {f: npz[f][[order[r["id"]] for r in rows]] for f in ("whole", "gameplay", "world")}
    lists, extra = rankings(rows, emb)

    spread = {}
    for k in ("whole", "gameplay", "world", "tags"):
        m = extra["sims"][k][~np.eye(len(rows), dtype=bool)]
        spread[k] = {"mean": round(float(m.mean()), 3), "std": round(float(m.std()), 3)}
    hub = {v: hubness(lists[v], len(rows)) for v in VARIANTS}
    for v in hub.values():
        v["top_hubs"] = [(rows[j]["slug"], c) for j, c in v["top_hubs"]]
    coverage = {v: sum(len(x) == TOP_K for x in lists[v]) / len(rows) for v in VARIANTS}
    overlap = {f"{a}∩{b}": round(float(np.mean([len(set(x) & set(y)) / TOP_K for x, y in zip(lists[a], lists[b])])), 2)
               for a in VARIANTS for b in VARIANTS if a < b}

    queries = pick_queries(rows, n_queries)
    rng = random.Random(11)
    packet, key = [], {}
    for qi in queries:
        pool = sorted({j for v in VARIANTS for j in lists[v][qi]})
        rng.shuffle(pool)
        cands = []
        for c, j in enumerate(pool):
            cid = f"{rows[qi]['slug']}#{c}"
            key[cid] = {"slug": rows[j]["slug"], "ranks": {v: lists[v][qi].index(j) for v in VARIANTS
                                                           if j in lists[v][qi]}}
            cands.append({"cid": cid, **card(rows[j])})
        packet.append({"query": rows[qi]["slug"], **card(rows[qi], 700), "candidates": cands})

    lists_named = {v: {rows[i]["slug"]: [rows[j]["slug"] for j in lst] for i, lst in enumerate(lists[v])}
                   for v in VARIANTS}
    (out / "rankings.json").write_text(json.dumps(
        {"weights": WEIGHTS, "spread": spread, "hubness": hub, "coverage": coverage, "overlap": overlap,
         "series_blocks": {rows[i]["slug"]: [rows[j]["slug"] for j in b] for i, b in enumerate(extra["series"]) if b},
         "lists": lists_named}, ensure_ascii=False, indent=1))
    (out / "blind_packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=1))
    (out / "blind_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1))
    pairs = sum(len(p["candidates"]) for p in packet)
    print(json.dumps({"games": len(rows), "queries": len(queries), "pairs": pairs, "spread": spread,
                      "hubness": hub, "coverage": coverage, "overlap": overlap}, ensure_ascii=False, indent=1))


# ---------- stage 4: metrics from blind grades ----------

def ndcg(gains: list[float], ideal: list[float]) -> float:
    dcg = sum(g / math.log2(r + 2) for r, g in enumerate(gains))
    idcg = sum(g / math.log2(r + 2) for r, g in enumerate(sorted(ideal, reverse=True)[:TOP_K]))
    return dcg / idcg if idcg else 0.0


def stage_finalize(out: Path) -> None:
    grades: dict[str, int] = json.loads((out / "grades.json").read_text())
    key = json.loads((out / "blind_key.json").read_text())
    packet = json.loads((out / "blind_packet.json").read_text())
    per_query: dict[str, dict] = {}
    agg = {v: defaultdict(list) for v in VARIANTS}
    for q in packet:
        cids = [c["cid"] for c in q["candidates"]]
        ideal = [grades[c] for c in cids]
        row = {}
        for v in VARIANTS:
            ranked = sorted((key[c]["ranks"][v], grades[c]) for c in cids if v in key[c]["ranks"])
            gains = [g for _, g in ranked] + [0] * (TOP_K - len(ranked))
            m = {"ndcg@5": ndcg(gains, ideal), "mean_grade": sum(gains) / TOP_K,
                 "relevant@5": sum(g >= 1 for g in gains) / TOP_K, "strong@5": sum(g == 2 for g in gains) / TOP_K,
                 "first_hit": gains[0] >= 1}
            for k, x in m.items():
                agg[v][k].append(float(x))
            row[v] = round(m["ndcg@5"], 3)
        per_query[q["query"]] = row
    summary = {v: {k: round(sum(x) / len(x), 3) for k, x in agg[v].items()} for v in VARIANTS}
    wins = Counter()
    for row in per_query.values():
        best = max(row.values())
        wins.update(v for v, x in row.items() if x == best)
    # paired bootstrap, V3 against every other variant: how often V3 is not better on a resampled query set
    rng = random.Random(3)
    qs = list(per_query)
    boot = {}
    for v in VARIANTS[:-1]:
        diffs = [per_query[q]["V3"] - per_query[q][v] for q in qs]
        worse = sum(sum(rng.choice(diffs) for _ in diffs) <= 0 for _ in range(5000)) / 5000
        boot[f"V3-{v}"] = {"mean_diff": round(sum(diffs) / len(diffs), 3), "p_not_better": round(worse, 3)}
    result = {"summary": summary, "wins": dict(wins), "bootstrap": boot, "per_query": per_query,
              "grade_counts": dict(Counter(grades.values()))}
    (out / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["essence", "embed", "pool", "finalize"])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--embed-model", default=EMBED_MODEL)
    ap.add_argument("--limit", type=int, help="essence: only this many games (smoke test)")
    a = ap.parse_args()
    out = a.out or RESULTS / f"similarity-{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"out: {out}")
    if a.stage == "essence":
        asyncio.run(stage_essence(out, a.limit))
    elif a.stage == "embed":
        asyncio.run(stage_embed(out, a.embed_model))
    elif a.stage == "pool":
        stage_pool(out, a.queries)
    else:
        stage_finalize(out)


if __name__ == "__main__":
    main()
