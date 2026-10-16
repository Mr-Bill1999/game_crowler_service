"""Token economics of the analysis lifecycle, replayed hour by hour on real review dates from the database.

  cd backend && .venv/bin/python -m eval.economics [--start 2026-09-15] [--end 2026-10-15] [--out eval/results/economics.json]

Each game is polled on the schedule of its tier (app.pipeline.tiers.compute_tier). At every poll the analysis
trigger (app.pipeline.tiers.needs_analysis) decides whether to run the three LLM tasks. Reviews of a given date
are spread evenly over that day. Token counts come from the model comparison run (gemini-3.5-flash-lite):
about 3.95 characters per input token, ~500 output tokens per critic summary, ~580 per player summary,
~345 per essence. Four strategies are compared:

  current      as implemented: trigger on Metacritic counts (ratings included), all three tasks re-read everything
  text         trigger on stored review texts only; re-run only the summaries whose reviews changed
  incremental  as "text", but a re-run reads only new reviews plus the previous summary
  daily        as "incremental", but at most one re-analysis per game per day
  lean         as "daily", plus: player reviews under 50 words are not sent to the model (their scores still go in as
               statistics), a full read takes at most 300 player reviews (stratified sample), and once a week, while
               the game is under 45 days old, the stored daily summaries are merged again from scratch (a reduce
               over summaries, not over raw reviews) so the rolling summary cannot drift
"""

import argparse
import asyncio
import json
import math
import re
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.ai.prompts import CHUNK_CHARS, SYSTEM, format_review
from app.core.config import Settings
from app.core.db import SessionLocal
from app.models import Game, Review
from app.pipeline.tiers import TierInput, compute_tier

CHARS_PER_TOKEN = 3.95
CALL_OVERHEAD = math.ceil((len(SYSTEM) + 450) / CHARS_PER_TOKEN)  # system prompt + game header
OUT = {"critic": 500, "user": 580, "essence": 345, "chunk": 550, "reduce": 600}
ESSENCE_IN = 1250  # description + both summaries, measured mean
SUMMARY_TOKENS = 650  # previous summary fed back in incremental mode
MIN_TEXTS = {"critic": 3, "user": 5}
STRATEGIES = ("current", "text", "incremental", "daily", "lean")
SUBSTANTIVE_WORDS = 50
SAMPLE_CAP = 300  # player reviews per full read
STATS_TOKENS = 150  # score histogram of all ratings, sent with every player summary in "lean"
FULL_REFRESH = timedelta(days=7)
PRICES = {  # USD per 1M tokens: standard in/out, batch in/out
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.125, 0.75),
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.15, 1.25),
    "gemini-3.8-flash": (0.75, 3.75, 0.375, 1.875),
    "gemini-3.8-flash 2027": (1.50, 7.50, 0.75, 3.75),
}


@dataclass
class GameSeries:
    slug: str
    release: date
    ratings_per_text: float  # Metacritic user ratings per stored text review
    # hour index -> (kind, chars) arrivals
    arrivals: dict[int, list[tuple[str, int, bool]]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    analyses: int = 0
    first: int = 0

    def add(self, tin: int, tout: int, calls: int) -> None:
        self.tokens_in += tin
        self.tokens_out += tout
        self.calls += calls


def summary_cost(chars: int, kind: str) -> tuple[int, int, int]:
    """(tokens_in, tokens_out, calls) for summarising `chars` of formatted reviews, map-reduce above CHUNK_CHARS."""
    chunks = max(1, math.ceil(chars / CHUNK_CHARS))
    tin = round(chars / CHARS_PER_TOKEN) + chunks * CALL_OVERHEAD
    if chunks == 1:
        return tin, OUT[kind], 1
    reduce_in = chunks * OUT["chunk"] + CALL_OVERHEAD
    return tin + reduce_in, chunks * OUT["chunk"] + OUT["reduce"], chunks + 1


async def load_series(start: date) -> list[GameSeries]:
    async with SessionLocal() as s:
        games = (await s.execute(select(Game).where(Game.release_date.is_not(None)))).scalars().all()
        texts = dict((await s.execute(select(Review.game_id, func.count()).where(Review.kind == "user")
                                      .group_by(Review.game_id))).all())
        out = []
        for g in games:
            ser = GameSeries(g.slug, g.release_date, min(10.0, max(1.0, g.user_count / max(texts.get(g.id, 0), 1))))
            reviews = (await s.execute(select(Review).where(Review.game_id == g.id))).scalars().all()
            for r in reviews:
                day = max(r.date or g.release_date, g.release_date, start)
                base = (day - start).days * 24
                # spread a day's reviews over its 24 hours, deterministically
                hour = base + zlib.crc32(r.id.encode()) % 24
                substantive = r.kind == "critic" or len(re.findall(r"\w+", r.quote or "")) >= SUBSTANTIVE_WORDS
                ser.arrivals[hour].append((r.kind, len(format_review(r)) + 1, substantive))
            if reviews:
                out.append(ser)
    return out


def simulate(series: list[GameSeries], start: date, end: date, strategy: str, s: Settings) -> dict:
    hours = (end - start).days * 24 + 24
    t0 = datetime(start.year, start.month, start.day, tzinfo=UTC)
    per_day: dict[date, Usage] = defaultdict(Usage)
    per_game: dict[str, Usage] = defaultdict(Usage)
    by_age: dict[str, Usage] = defaultdict(Usage)
    for g in series:
        texts = {"critic": 0, "user": 0}
        chars = {"critic": 0, "user": 0}
        at_last = {"critic": (0, 0), "user": (0, 0)}  # (texts, chars) at the last summary of this kind
        batches = {"critic": 0, "user": 0}  # stored incremental summaries, merged by the weekly refresh
        analyzed_count, analyzed_at, last_day, last_full = 0, None, None, None
        release_hour = max(0, (g.release - start).days * 24)
        next_check = release_hour
        last_growth = None
        prev_total = 0
        for h in range(hours):
            for kind, c, substantive in g.arrivals.get(h, ()):
                if strategy == "lean" and not substantive:
                    continue
                texts[kind] += 1
                chars[kind] += c
            if h < next_check:
                continue
            now = t0 + timedelta(hours=h)
            if strategy == "current":
                counted = texts["critic"] + round(texts["user"] * g.ratings_per_text)
            else:
                counted = texts["critic"] + texts["user"]
            if counted > prev_total:
                last_growth = now
            prev_total = counted

            due = (texts["critic"] >= MIN_TEXTS["critic"] or texts["user"] * (
                g.ratings_per_text if strategy == "current" else 1) >= s.analysis_min_user)
            if due and analyzed_at is not None:
                cooldown = now - analyzed_at < timedelta(hours=s.reanalyze_cooldown_hours)
                new = counted - analyzed_count
                due = not cooldown and (new >= s.reanalyze_min_new or new >= s.reanalyze_ratio * max(analyzed_count, 1))
                if strategy in ("daily", "lean") and last_day == now.date():
                    due = False
            if due:
                tin = tout = calls = 0
                age = (now.date() - g.release).days
                refresh = (strategy == "lean" and last_full is not None and now - last_full >= FULL_REFRESH
                           and age <= s.warm_days)
                for kind in ("critic", "user"):
                    if texts[kind] < MIN_TEXTS[kind]:
                        continue
                    if strategy != "current" and analyzed_at is not None and texts[kind] == at_last[kind][0]:
                        continue  # nothing new for this summary
                    full = (strategy not in ("incremental", "daily", "lean") or analyzed_at is None
                            or not at_last[kind][0])
                    if refresh and not full:
                        delta = summary_cost(chars[kind] - at_last[kind][1], kind)
                        merge_in = (batches[kind] + 2) * OUT["chunk"] + CALL_OVERHEAD
                        a, b, n = delta[0] + merge_in, delta[1] + OUT["reduce"], delta[2] + 1
                        batches[kind] = 0
                    elif full:
                        c = chars[kind]
                        if strategy == "lean" and kind == "user" and texts[kind] > SAMPLE_CAP:
                            c = round(SAMPLE_CAP * chars[kind] / texts[kind])
                        a, b, n = summary_cost(c, kind)
                    else:
                        a, b, n = summary_cost(chars[kind] - at_last[kind][1], kind)
                        a += SUMMARY_TOKENS
                        batches[kind] += 1
                    if strategy == "lean" and kind == "user":
                        a += STATS_TOKENS
                    tin, tout, calls = tin + a, tout + b, calls + n
                    at_last[kind] = (texts[kind], chars[kind])
                if calls:
                    tin, tout, calls = tin + ESSENCE_IN, tout + OUT["essence"], calls + 1
                    first = analyzed_at is None
                    if first or refresh:
                        last_full = now
                    bucket = "0–7 дней" if age <= 7 else "8–14 дней" if age <= 14 else "15–30 дней"
                    for u in (per_day[now.date()], per_game[g.slug], by_age[bucket], by_age[f"{now.date()}|{bucket}"]):
                        u.add(tin, tout, calls)
                        u.analyses += 1
                        u.first += first
                    analyzed_count, analyzed_at, last_day = counted, now, now.date()
            _, nxt = compute_tier(TierInput(g.release, t0 + timedelta(hours=release_hour), counted, last_growth,
                                               None), now, s)
            next_check = h + 24 * 365 if nxt is None else h + max(1, round((nxt - now).total_seconds() / 3600))
    return {"per_day": per_day, "per_game": per_game, "by_age": by_age}


def cost(tin: float, tout: float, model: str, batch: bool = False) -> float:
    p = PRICES[model]
    pin, pout = (p[2], p[3]) if batch else (p[0], p[1])
    return (tin * pin + tout * pout) / 1e6


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 15))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 14))
    ap.add_argument("--steady-days", type=int, default=14)
    ap.add_argument("--out", default="eval/results/economics.json")
    a = ap.parse_args()
    s = Settings()
    series = await load_series(a.start)
    steady_from = a.end - timedelta(days=a.steady_days - 1)
    result = {"window": [a.start.isoformat(), a.end.isoformat()], "steady_from": steady_from.isoformat(),
              "games_with_reviews": len(series), "strategies": {}}
    for strategy in STRATEGIES:
        sim = simulate(series, a.start, a.end, strategy, s)
        days = [d for d in sorted(sim["per_day"]) if d <= a.end]
        steady = [sim["per_day"][d] for d in days if d >= steady_from]
        n = a.steady_days
        avg_in = sum(u.tokens_in for u in steady) / n
        avg_out = sum(u.tokens_out for u in steady) / n
        peak_day = max(days, key=lambda d: sim["per_day"][d].tokens_in)
        top = sorted(sim["per_game"].items(), key=lambda kv: -kv[1].tokens_in)[:8]
        result["strategies"][strategy] = {
            "avg_tokens_in": round(avg_in), "avg_tokens_out": round(avg_out),
            "avg_calls": round(sum(u.calls for u in steady) / n, 1),
            "avg_analyses": round(sum(u.analyses for u in steady) / n, 1),
            "avg_first_analyses": round(sum(u.first for u in steady) / n, 1),
            "peak_day": peak_day.isoformat(), "peak_tokens_in": sim["per_day"][peak_day].tokens_in,
            "daily": [{"day": d.isoformat(), "in": sim["per_day"][d].tokens_in, "out": sim["per_day"][d].tokens_out,
                       "analyses": sim["per_day"][d].analyses, "first": sim["per_day"][d].first} for d in days],
            "top_games": [{"slug": k, "in": v.tokens_in, "analyses": v.analyses} for k, v in top],
            "by_age": {k: {"in": v.tokens_in, "analyses": v.analyses} for k, v in sim["by_age"].items() if "|" not in k},
            "by_age_steady": {b: round(sum(v.tokens_in for k, v in sim["by_age"].items()
                                           if "|" in k and k.split("|")[1] == b and k.split("|")[0] >= steady_from.isoformat()) / n)
                              for b in ("0–7 дней", "8–14 дней", "15–30 дней")},
            "cost_per_day": {m: {"standard": round(cost(avg_in, avg_out, m), 3), "batch": round(cost(avg_in, avg_out, m, True), 3)}
                             for m in PRICES},
        }
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for k, v in result["strategies"].items():
        print(f"{k:12} in/day {v['avg_tokens_in']:>10,}  out/day {v['avg_tokens_out']:>8,}  analyses/day {v['avg_analyses']:>5}"
              f" (first {v['avg_first_analyses']})  calls/day {v['avg_calls']:>6}  peak {v['peak_tokens_in']:>10,} on {v['peak_day']}"
              f"  $/day 3.5-lite {v['cost_per_day']['gemini-3.5-flash-lite']['standard']}")


if __name__ == "__main__":
    asyncio.run(main())
