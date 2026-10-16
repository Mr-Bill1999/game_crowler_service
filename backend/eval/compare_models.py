"""Blind comparison of Gemini models on real games from the database: quality (judged) versus cost.

  cd backend && .venv/bin/python -m eval.compare_models --games 12
  .venv/bin/python -m eval.compare_models --resume eval/results/<run>      # continue an interrupted run

Every candidate runs the production analysis (critic summary, player summary, essence) on the same games.
A stronger model then rates the anonymised, shuffled outputs of each game and task against the source reviews.
Outputs are cached per model and game, so a rerun only pays for what is missing.
"""

import argparse
import asyncio
import json
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.ai.analyze import analyze_game, load_reviews
from app.ai.gemini import PRICES, GeminiProvider, cost_usd
from app.ai.prompts import format_review, game_header
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Game, Review
from eval.report import build_report

# gemini-2.5-flash-lite and gemini-2.5-flash are listed but return 404 for new API keys (15.10.2026).
CANDIDATES: list[tuple[str, str | None]] = [
    ("gemini-2.5-flash-lite", None),
    ("gemini-3.1-flash-lite", "minimal"),
    ("gemini-3.5-flash-lite", "minimal"),
    ("gemini-2.5-flash", "low"),
    ("gemini-3-flash-preview", "minimal"),
    ("gemini-3.8-flash", "low"),
]
# Pro-class judges need a paid key: on the free tier their daily quota is zero.
JUDGES: list[tuple[str, str | None]] = [("gemini-4-argon", None), ("gemini-3.1-pro-preview", None)]
JUDGE_SOURCE_CHARS = 400_000
MONTHLY_GAMES = 300  # analyses per month, from the design doc estimate

JUDGE_SYSTEM = """Ты строгий редактор, который проверяет автоматические разборы отзывов о видеоиграх.
Тебе дают исходные данные и несколько вариантов разбора под буквами. Варианты написаны разными системами,
порядок случайный. Оцени каждый вариант отдельно по шкале 1-5 (5 — отлично), сверяясь с исходными данными:

- faithfulness: каждое утверждение подтверждается источником; выдуманные факты, перепутанные оценки и
  приписанные отзывам мнения снижают балл сильнее всего;
- coverage: отражены главные и самые частые плюсы и минусы, пропорции мнений переданы верно;
- specificity: конкретика об этой игре (механики, технические проблемы, сюжет), а не общие фразы;
- clarity: понятный грамотный русский язык, нет повторов и воды.

В hallucinations перечисли конкретные утверждения варианта, которых нет в источнике (пустой список, если таких нет).
Сравнивай варианты между собой: если один заметно лучше, это должно быть видно по баллам."""

_SCORE = {"type": "integer", "minimum": 1, "maximum": 5}
JUDGE_SCHEMA = {
    "type": "object",
    "required": ["ratings"],
    "properties": {"ratings": {"type": "array", "items": {
        "type": "object",
        "required": ["label", "faithfulness", "coverage", "specificity", "clarity", "hallucinations", "comment"],
        "properties": {"label": {"type": "string"}, "faithfulness": _SCORE, "coverage": _SCORE,
                       "specificity": _SCORE, "clarity": _SCORE,
                       "hallucinations": {"type": "array", "items": {"type": "string"}},
                       "comment": {"type": "string"}},
    }}},
}
CRITERIA = ["faithfulness", "coverage", "specificity", "clarity"]


async def pick_games(n: int) -> list[Game]:
    """From thousands of player reviews down to a handful of critics."""
    async with SessionLocal() as s:
        counts = (select(Review.game_id, func.count().label("n"),
                         func.count().filter(Review.kind == "critic").label("critic"))
                  .group_by(Review.game_id).subquery())
        rows = (await s.execute(select(Game, counts.c.n).join(counts, counts.c.game_id == Game.id)
                                .where(counts.c.critic >= 3).order_by(counts.c.n.desc())
                                .options(selectinload(Game.platforms)))).all()
    ranked = [g for g, _ in rows]
    if len(ranked) <= n:
        return ranked
    # Models differ most on big review sets, so most of the sample is the largest games; the rest spans sizes.
    big = n * 3 // 5
    rest, k = ranked[big:], n - big
    return ranked[:big] + [rest[round(i * (len(rest) - 1) / max(k - 1, 1))] for i in range(k)]


async def available_models(provider: GeminiProvider) -> set[str]:
    names = set()
    async for m in await provider.client.aio.models.list():
        names.add(m.name.removeprefix("models/"))
    return names


async def probe(provider: GeminiProvider, model: str, thinking: str | None) -> str | None:
    """Listed is not the same as usable: some models answer 404 to new keys. Returns the reason if unusable."""
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    try:
        await provider.generate_json(system="Ответь JSON.", prompt="ok=true", schema=schema, model=model,
                                     thinking_level=thinking)
    except Exception as e:  # noqa: BLE001  any failure means the model is out of the comparison
        return f"{type(e).__name__}: {str(e)[:160]}"
    return None


async def run_candidate(provider, model, thinking, game, reviews, out_dir: Path, gate) -> dict:
    path = out_dir / "outputs" / model / f"{game.slug}.json"
    if path.exists():
        return json.loads(path.read_text())
    async with gate:
        res = await analyze_game(provider, game, reviews["critic"], reviews["user"], model, thinking)
    record = {"model": model, "thinking": thinking, "slug": game.slug, "tasks": {
        k: {"data": t.data, "error": t.error, "calls": len(t.calls), "reviews_seen": t.reviews_seen,
            "tokens_in": t.tokens_in, "tokens_out": t.tokens_out,
            "thinking_tokens": sum(c.thinking_tokens for c in t.calls),
            "cost_usd": t.cost_usd, "latency_s": t.latency_s}
        for k, t in res.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    print(f"  {model:24} {game.slug:40} ${sum(t['cost_usd'] for t in record['tasks'].values()):.4f}")
    return record


def judge_source(game: Game, task: str, reviews: dict[str, list[Review]]) -> str:
    if task == "essence":
        lines = [format_review(r) for r in reviews["critic"]]
        body = f"Описание:\n{game.description or '—'}\n\nРецензии критиков:\n" + "\n".join(lines)
    else:
        lines = [format_review(r) for r in reviews[task]]
        if sum(map(len, lines)) > JUDGE_SOURCE_CHARS:  # random but fixed sample for very large sets
            random.Random(game.id).shuffle(lines)
            kept, size = [], 0
            for line in lines:
                if size + len(line) > JUDGE_SOURCE_CHARS:
                    break
                kept.append(line)
                size += len(line)
            body = (f"Отзывы ({len(kept)} из {len(lines)}, случайная выборка; разборы делались по всем):\n"
                    + "\n".join(kept))
        else:
            body = f"Отзывы ({len(lines)}):\n" + "\n".join(lines)
    return f"{game_header(game)}\n\n{body}"


async def judge_task(provider, judge_model, judge_thinking, game, task, records, reviews, out_dir: Path,
                     gate) -> dict | None:
    path = out_dir / "judgements" / f"{game.slug}.{task}.json"
    if path.exists():
        return json.loads(path.read_text())
    entries = [(r["model"], r["tasks"][task]["data"]) for r in records if r["tasks"][task]["data"]]
    if len(entries) < 2:
        return None
    random.Random(f"{game.slug}:{task}").shuffle(entries)
    labels = [chr(ord("A") + i) for i in range(len(entries))]
    variants = "\n\n".join(f"Вариант {lbl}:\n{json.dumps(d, ensure_ascii=False)}" for lbl, (_, d) in zip(labels, entries))
    what = {"critic": "разбор рецензий критиков", "user": "разбор отзывов игроков", "essence": "описание сути игры"}[task]
    prompt = f"Исходные данные:\n{judge_source(game, task, reviews)}\n\nЗадача вариантов: {what}.\n\n{variants}\n\nОцени каждый вариант."
    async with gate:
        r = await provider.generate_json(system=JUDGE_SYSTEM, prompt=prompt, schema=JUDGE_SCHEMA, model=judge_model,
                                         thinking_level=judge_thinking)
    by_label = {x["label"].strip().upper()[:1]: x for x in r.data["ratings"]}
    result = {"slug": game.slug, "task": task, "judge": judge_model, "cost_usd": r.cost_usd,
              "ratings": {model: by_label.get(lbl) for lbl, (model, _) in zip(labels, entries)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"  judged {game.slug:40} {task}")
    return result


def aggregate(models, records, judgements) -> list[dict]:
    rows = []
    for model, thinking in models:
        mine = [r for r in records if r["model"] == model]
        tasks = [t for r in mine for t in r["tasks"].values() if t["reviews_seen"] or t["data"] or t["error"]]
        per_game_cost = [sum(t["cost_usd"] for t in r["tasks"].values()) for r in mine]
        per_game_latency = [max(r["tasks"]["critic"]["latency_s"], r["tasks"]["user"]["latency_s"])
                            + r["tasks"]["essence"]["latency_s"] for r in mine]
        scores = {c: [] for c in CRITERIA}
        overall, halluc = [], 0
        for j in judgements:
            rating = j["ratings"].get(model)
            if rating:
                for c in CRITERIA:
                    scores[c].append(rating[c])
                overall.append(statistics.mean(rating[c] for c in CRITERIA))
                halluc += len(rating.get("hallucinations") or [])
        avg_cost = statistics.mean(per_game_cost) if per_game_cost else 0
        p = PRICES.get(model, (0, 0, 0, 0))
        rows.append({
            "model": model, "thinking": thinking or "выкл.", "price_in": p[0], "price_out": p[1],
            "quality": round(statistics.mean(overall), 2) if overall else None,
            **{c: round(statistics.mean(v), 2) if v else None for c, v in scores.items()},
            "hallucinations": halluc, "judged": len(overall),
            "failures": sum(1 for t in tasks if t["error"] and "429" not in t["error"]),
            "quota_skipped": sum(1 for t in tasks if t["error"] and "429" in t["error"]),
            "tokens_in": sum(t["tokens_in"] for t in tasks), "tokens_out": sum(t["tokens_out"] for t in tasks),
            "thinking_tokens": sum(t.get("thinking_tokens", 0) for t in tasks),
            "cost_total": round(sum(per_game_cost), 4), "cost_per_game": round(avg_cost, 5),
            "cost_per_game_median": round(statistics.median(per_game_cost), 5) if per_game_cost else 0,
            "monthly": round(avg_cost * MONTHLY_GAMES, 2),
            "monthly_batch": round(sum(cost_usd(model, t["tokens_in"], t["tokens_out"], batch=True) for t in tasks)
                                   / max(len(mine), 1) * MONTHLY_GAMES, 2),
            "latency_p50": round(statistics.median(per_game_latency), 1) if per_game_latency else None,
        })
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=12)
    ap.add_argument("--resume", type=Path)
    ap.add_argument("--models", nargs="*", help="subset of candidate model ids")
    ap.add_argument("--concurrency", type=int, default=3, help="games in flight per model")
    ap.add_argument("--judge", help="judge model, optionally with thinking level: gemini-3.6-flash:high")
    ap.add_argument("--no-judge", action="store_true", help="only generate candidate outputs")
    a = ap.parse_args()

    s = get_settings()
    if not s.gemini_api_key:
        raise SystemExit("Нет ключа: добавьте GA_GEMINI_API_KEY=... в backend/.env")
    provider = GeminiProvider(s.gemini_api_key, max_attempts=8)  # free-tier keys hit per-minute limits
    out_dir = a.resume or Path("eval/results") / datetime.now(UTC).strftime("%Y%m%d-%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)

    have = await available_models(provider)
    wanted = [(m, t) for m, t in CANDIDATES if (not a.models or m in a.models)]
    reasons = await asyncio.gather(*(probe(provider, m, t) if m in have else asyncio.sleep(0, "нет в списке")
                                     for m, t in wanted))
    for (m, _), r in zip(wanted, reasons):
        if r:
            print(f"  {m} unavailable: {r}")
    models = [(m, t) for (m, t), r in zip(wanted, reasons) if r is None]
    missing = [m for (m, _), r in zip(wanted, reasons) if r is not None]
    judges = [tuple((a.judge.split(":") + [None])[:2])] if a.judge else JUDGES
    judge, judge_thinking = None, None
    if not a.no_judge:
        for j, t in judges:
            if j in have and await probe(provider, j, t) is None:
                judge, judge_thinking = j, t
                break
    print(f"models: {[m for m, _ in models]}; unavailable: {missing}; judge: {judge} {judge_thinking or ''}")
    if not models or (not judge and not a.no_judge):
        raise SystemExit("Ключ не видит нужных моделей или судью")

    games = await pick_games(a.games)
    (out_dir / "sample.json").write_text(json.dumps([g.slug for g in games]))
    reviews = {}
    async with SessionLocal() as session:
        for g in games:
            reviews[g.id] = {"critic": await load_reviews(session, g.id, "critic"),
                             "user": await load_reviews(session, g.id, "user")}
    print("games:", ", ".join(f"{g.slug} ({len(reviews[g.id]['critic'])}+{len(reviews[g.id]['user'])})" for g in games))

    gates = {m: asyncio.Semaphore(a.concurrency) for m, _ in models}
    records = await asyncio.gather(*(run_candidate(provider, m, t, g, reviews[g.id], out_dir, gates[m])
                                     for m, t in models for g in games))
    if a.no_judge:
        print(f"outputs ready in {out_dir}; judge them with --resume {out_dir} --judge <model>")
        return
    judge_gate = asyncio.Semaphore(4)
    judgements = [j for j in await asyncio.gather(*(
        judge_task(provider, judge, judge_thinking, g, task, [r for r in records if r["slug"] == g.slug], reviews[g.id], out_dir,
                   judge_gate)
        for g in games for task in ("critic", "user", "essence"))) if j]

    summary = aggregate(models, records, judgements)
    meta = {"run": out_dir.name, "judge": f"{judge} ({judge_thinking})" if judge_thinking else judge, "unavailable": missing, "monthly_games": MONTHLY_GAMES,
            "judge_cost": round(sum(j["cost_usd"] for j in judgements), 4),
            "games": [{"slug": g.slug, "title": g.title, "critic": len(reviews[g.id]["critic"]),
                       "user": len(reviews[g.id]["user"])} for g in games]}
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, "models": summary}, ensure_ascii=False, indent=1))
    report = build_report(meta, summary, records, judgements)
    (out_dir / "report.html").write_text(report)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"report: {out_dir / 'report.html'}")


if __name__ == "__main__":
    asyncio.run(main())
