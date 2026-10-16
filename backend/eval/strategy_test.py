"""Does reading less hurt the player summary? Three modes on the same games, judged blind.

  full         every player review (taken from a model comparison run, same model and prompt)
  lean         reviews of 50+ words, deduplicated, at most 300 sampled in score proportions, plus score statistics
  incremental  lean first read on the first two days after release, then one update per day:
               previous summary + that day's new substantive reviews

  cd backend && .venv/bin/python -m eval.strategy_test --full-run eval/results/20261015-0857
  .venv/bin/python -m eval.strategy_test --resume eval/results/strategy-<ts>     continue after a quota stop
  .venv/bin/python -m eval.strategy_test --modes incremental --reuse eval/results/strategy-<old>
                                         rerun some modes, take the others from an earlier run
"""

import argparse
import asyncio
import json
import random
import shutil
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.analyze import load_reviews
from app.ai.base import REVIEW_SUMMARY_SCHEMA
from app.ai.gemini import GeminiProvider
from app.ai.prompts import SYSTEM, UPDATE_SYSTEM, format_review, game_header, lean_summary_prompt, update_prompt
from app.ai.selection import score_stats, stratified_sample, substantive
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Game, RawResponse

MODEL, THINKING = "gemini-3.5-flash-lite", "minimal"
GAMES = ["marvels-wolverine", "gears-of-war-e-day", "control-resonant"]
EXCERPT_CHARS = 18_000


async def metacritic_user_summary(slug: str) -> dict | None:
    async with SessionLocal() as s:
        raw = await s.scalar(select(RawResponse.body).where(RawResponse.kind == "composer", RawResponse.key == slug))
    if not raw:
        return None
    comps = {c["meta"]["componentName"]: c.get("data") or {} for c in raw["components"]}
    return (comps.get("user-score-summary") or {}).get("item")


async def run_lean(p: GeminiProvider, g: Game, reviews, mc) -> dict:
    sub = substantive(reviews)
    sample = stratified_sample(sub, reviews, seed=g.id)
    r = await p.generate_json(system=SYSTEM, prompt=lean_summary_prompt(
        g, "user", [format_review(x) for x in sample], score_stats(reviews, mc), len(sub)),
        schema=REVIEW_SUMMARY_SCHEMA, model=MODEL, thinking_level=THINKING)
    return {"data": r.data, "calls": 1, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cost_usd": r.cost_usd,
            "read_reviews": len(sample), "substantive": len(sub), "all": len(reviews)}


async def run_incremental(p: GeminiProvider, g: Game, reviews, mc, path: Path) -> dict:
    """One update per calendar day, resumable: progress is saved after every call."""
    state = json.loads(path.read_text()) if path.exists() else None
    by_day = defaultdict(list)
    for r in reviews:
        by_day[max(r.date or g.release_date, g.release_date)].append(r)
    days = sorted(by_day)
    first_until = g.release_date + timedelta(days=1)
    if state is None:
        seen = [r for d in days if d <= first_until for r in by_day[d]]
        sub = substantive(seen)
        sample = stratified_sample(sub, seen, seed=g.id)
        r = await p.generate_json(system=SYSTEM, prompt=lean_summary_prompt(
            g, "user", [format_review(x) for x in sample], score_stats(seen), len(sub)),
            schema=REVIEW_SUMMARY_SCHEMA, model=MODEL, thinking_level=THINKING)
        state = {"data": r.data, "calls": 1, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                 "cost_usd": r.cost_usd, "read_reviews": len(sample), "done_until": first_until.isoformat(),
                 "trajectory": [{"day": first_until.isoformat(), "new": len(sample), "tokens_in": r.tokens_in}]}
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    seen_keys = {x.id for d in days if d.isoformat() <= state["done_until"] for x in by_day[d]}
    for d in days:
        if d.isoformat() <= state["done_until"]:
            continue
        upto = [r for dd in days if dd <= d for r in by_day[dd]]
        new = [r for r in substantive(upto) if r.id not in seen_keys and r.date and r.date <= d]
        seen_keys |= {r.id for r in by_day[d]}
        if new:
            new = stratified_sample(new, by_day[d], seed=g.id + d.toordinal())
            final = d == days[-1]
            r = await p.generate_json(system=UPDATE_SYSTEM, prompt=update_prompt(
                g, "user", state["data"], [format_review(x) for x in new], score_stats(upto, mc if final else None)),
                schema=REVIEW_SUMMARY_SCHEMA, model=MODEL, thinking_level=THINKING)
            state["data"] = r.data
            state["calls"] += 1
            state["tokens_in"] += r.tokens_in
            state["tokens_out"] += r.tokens_out
            state["cost_usd"] += r.cost_usd
            state["read_reviews"] += len(new)
            state["trajectory"].append({"day": d.isoformat(), "new": len(new), "tokens_in": r.tokens_in})
        state["done_until"] = d.isoformat()
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    return state


def excerpt(reviews, seed: int) -> str:
    """What the judge sees: a fixed random sample of all reviews, short ones included."""
    lines = [format_review(r) for r in reviews]
    random.Random(seed).shuffle(lines)
    kept, size = [], 0
    for line in lines:
        if size + len(line) <= EXCERPT_CHARS:
            kept.append(line)
            size += len(line)
    return f"Отзывы игроков ({len(kept)} из {len(lines)}, случайная выборка):\n" + "\n".join(kept)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-run", type=Path, default=Path("eval/results/20261015-0857"))
    ap.add_argument("--resume", type=Path)
    ap.add_argument("--games", nargs="*", default=GAMES)
    ap.add_argument("--modes", nargs="*", default=["lean", "incremental"], choices=["lean", "incremental"])
    ap.add_argument("--reuse", type=Path, help="take modes not in --modes from this earlier run")
    a = ap.parse_args()
    out = a.resume or Path("eval/results") / f"strategy-{datetime.now(UTC):%Y%m%d-%H%M}"
    (out / "outputs").mkdir(parents=True, exist_ok=True)
    p = GeminiProvider(get_settings().gemini_api_key, max_attempts=8)

    async def one(slug: str) -> None:
        async with SessionLocal() as s:
            g = (await s.execute(select(Game).where(Game.slug == slug).options(selectinload(Game.platforms)))).scalar_one()
            reviews = await load_reviews(s, g.id, "user")
        mc = await metacritic_user_summary(slug)
        full = json.loads((a.full_run / "outputs" / MODEL / f"{slug}.json").read_text())["tasks"]["user"]
        (out / "outputs" / f"{slug}.full.json").write_text(json.dumps(
            {**{k: full[k] for k in ("data", "calls", "tokens_in", "tokens_out", "cost_usd")}, "read_reviews": len(reviews)},
            ensure_ascii=False, indent=1))
        for mode in ("lean", "incremental"):
            path = out / "outputs" / f"{slug}.{mode}.json"
            if mode not in a.modes:
                if not path.exists() and a.reuse:
                    shutil.copy(a.reuse / "outputs" / f"{slug}.{mode}.json", path)
                continue
            if mode == "lean" and not path.exists():
                path.write_text(json.dumps(await run_lean(p, g, reviews, mc), ensure_ascii=False, indent=1))
            elif mode == "incremental":
                await run_incremental(p, g, reviews, mc, path)
            print(f"  {slug} {mode} done")

        modes = ["full", "lean", "incremental"]
        random.Random(f"{slug}:strategy").shuffle(modes)
        key = {chr(65 + i): m for i, m in enumerate(modes)}
        variants = "\n\n".join(
            f"### Вариант {lbl}\n" + json.dumps(json.loads((out / "outputs" / f"{slug}.{m}.json").read_text())["data"],
                                                 ensure_ascii=False, indent=1) for lbl, m in key.items())
        (out / "packets").mkdir(exist_ok=True)
        (out / "packets" / f"{slug}.md").write_text(
            f"# {g.title} — разбор отзывов игроков\n\n{game_header(g)}\n\n{score_stats(reviews, mc)}\n\n"
            f"## Источник\n{excerpt(reviews, g.id)}\n\n## Варианты\n\n{variants}\n")
        keys_path = out / ".key.json"
        keys = json.loads(keys_path.read_text()) if keys_path.exists() else {}
        keys[slug] = key
        keys_path.write_text(json.dumps(keys, ensure_ascii=False, indent=1))

    for slug in a.games:  # sequential: the free tier has per-minute limits
        await one(slug)
    print(f"packets in {out / 'packets'}")


if __name__ == "__main__":
    asyncio.run(main())
