"""Blind judging by a person (or an outside model) instead of a Gemini judge.

  python -m eval.blind packets  <run>     write one packet per game and task: source excerpt + shuffled variants
  python -m eval.blind finalize <run> --judge "Claude Opus 5.5"
                                         read <run>/blind/scores/*.json, unmask, aggregate and build the report

The label-to-model key lives in <run>/blind/.key.json; the judge reads only packets and writes scores.
Score file per packet: {"ratings": [{"label": "A", "faithfulness": 1-5, "coverage": 1-5, "specificity": 1-5,
"clarity": 1-5, "hallucinations": [...], "comment": "..."}]}
"""

import argparse
import asyncio
import json
import random
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.analyze import load_reviews
from app.ai.prompts import format_review, game_header
from app.core.db import SessionLocal
from app.models import Game
from eval.compare_models import CANDIDATES, MONTHLY_GAMES, aggregate
from eval.report import build_report

EXCERPT_CHARS = 18_000
TASKS = ("critic", "user", "essence")


def load_records(run: Path) -> list[dict]:
    """Outputs of the games in this run's sample; a resumed run may hold outputs of games dropped from it."""
    sample = set(json.loads((run / "sample.json").read_text()))
    records = [json.loads(p.read_text()) for p in sorted((run / "outputs").rglob("*.json"))]
    return [r for r in records if r["slug"] in sample]


def norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def quote_check(data: dict | None, source_norm: str) -> tuple[int, int]:
    """(verbatim, total) for notable_quotes; a quote counts if its normalised text occurs in the source."""
    quotes = (data or {}).get("notable_quotes") or []
    found = sum(1 for q in quotes if norm(q.strip("«»\"' …."))[:120] in source_norm)
    return found, len(quotes)


def excerpt(lines: list[str], seed: int) -> tuple[str, int]:
    if sum(map(len, lines)) <= EXCERPT_CHARS:
        return "\n".join(lines), len(lines)
    pool = lines[:]
    random.Random(seed).shuffle(pool)
    kept, size = [], 0
    for line in pool:
        if size + len(line) > EXCERPT_CHARS:
            continue
        kept.append(line)
        size += len(line)
    return "\n".join(kept), len(kept)


def render_variant(task: str, d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1)


async def write_packets(run: Path) -> None:
    records = load_records(run)
    slugs = sorted({r["slug"] for r in records})
    out = run / "blind"
    (out / "packets").mkdir(parents=True, exist_ok=True)
    (out / "sources").mkdir(exist_ok=True)
    (out / "scores").mkdir(exist_ok=True)
    key: dict[str, dict[str, str]] = {}
    auto: dict[str, dict] = {}
    async with SessionLocal() as s:
        for slug in slugs:
            g = (await s.execute(select(Game).where(Game.slug == slug).options(selectinload(Game.platforms)))).scalar_one()
            reviews = {k: await load_reviews(s, g.id, k) for k in ("critic", "user")}
            full = {k: [format_review(r) for r in v] for k, v in reviews.items()}
            for k, lines in full.items():
                (out / "sources" / f"{slug}.{k}.txt").write_text("\n".join(lines))
            source_norm = {k: norm("\n".join(v)) for k, v in full.items()}
            for task in TASKS:
                variants = [(r["model"], r["tasks"][task]["data"]) for r in records
                            if r["slug"] == slug and r["tasks"][task]["data"]]
                for r in records:
                    if r["slug"] == slug and task != "essence":
                        auto[f"{r['model']}|{slug}|{task}"] = dict(zip(
                            ("quotes_verbatim", "quotes_total"),
                            quote_check(r["tasks"][task]["data"], source_norm[task])))
                if len(variants) < 2:
                    continue
                random.Random(f"{slug}:{task}:blind").shuffle(variants)
                pid = f"{slug}.{task}"
                key[pid] = {chr(65 + i): m for i, (m, _) in enumerate(variants)}
                if task == "essence":
                    src, shown = excerpt(full["critic"], g.id)
                    source = f"Описание:\n{g.description or '—'}\n\nРецензии критиков ({shown} из {len(full['critic'])}):\n{src}"
                else:
                    src, shown = excerpt(full[task], g.id)
                    source = f"Отзывы ({shown} из {len(full[task])}; полный текст: blind/sources/{slug}.{task}.txt):\n{src}"
                what = {"critic": "разбор рецензий критиков", "user": "разбор отзывов игроков",
                        "essence": "описание сути игры"}[task]
                body = "\n\n".join(f"### Вариант {chr(65 + i)}\n{render_variant(task, d)}" for i, (_, d) in enumerate(variants))
                (out / "packets" / f"{pid}.md").write_text(
                    f"# {g.title} — {what}\n\n{game_header(g)}\n\n## Источник\n{source}\n\n## Варианты\n\n{body}\n")
    (out / ".key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1))
    (out / "auto_checks.json").write_text(json.dumps(auto, ensure_ascii=False, indent=1))
    print(f"{len(key)} packets in {out / 'packets'}")


async def finalize(run: Path, judge: str, recommended: str | None = None) -> None:
    records = load_records(run)
    key = json.loads((run / "blind" / ".key.json").read_text())
    auto = json.loads((run / "blind" / "auto_checks.json").read_text())
    judgements = []
    for pid, mapping in key.items():
        path = run / "blind" / "scores" / f"{pid}.json"
        if not path.exists():
            print("no score for", pid)
            continue
        slug, task = pid.rsplit(".", 1)
        ratings = {x["label"]: x for x in json.loads(path.read_text())["ratings"]}
        judgements.append({"slug": slug, "task": task, "judge": judge, "cost_usd": 0.0,
                           "ratings": {model: ratings.get(lbl) for lbl, model in mapping.items()}})
    models = [(m, t) for m, t in CANDIDATES if any(r["model"] == m for r in records)]
    rows = aggregate(models, records, judgements)
    for row in rows:
        mine = [v for k, v in auto.items() if k.startswith(row["model"] + "|")]
        total = sum(v["quotes_total"] for v in mine)
        row["quotes_verbatim"] = round(sum(v["quotes_verbatim"] for v in mine) / total, 2) if total else None
    summary_path = run / "summary.json"
    meta = json.loads(summary_path.read_text())["meta"] if summary_path.exists() else {}
    async with SessionLocal() as s:
        games = []
        for slug in sorted({r["slug"] for r in records}):
            g = (await s.execute(select(Game).where(Game.slug == slug))).scalar_one()
            games.append({"slug": slug, "title": g.title,
                          "critic": len(await load_reviews(s, g.id, "critic")),
                          "user": len(await load_reviews(s, g.id, "user"))})
    meta.update({"run": run.name, "judge": judge, "recommended": recommended, "monthly_games": MONTHLY_GAMES, "judge_cost": 0.0, "games": games})
    meta.setdefault("unavailable", [])
    summary_path.write_text(json.dumps({"meta": meta, "models": rows}, ensure_ascii=False, indent=1))
    for j in judgements:
        p = run / "judgements" / f"{j['slug']}.{j['task']}.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(j, ensure_ascii=False, indent=1))
    conclusion = (run / "conclusion.html").read_text() if (run / "conclusion.html").exists() else None
    (run / "report.html").write_text(build_report(meta, rows, records, judgements, conclusion))
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["packets", "finalize"])
    ap.add_argument("run", type=Path)
    ap.add_argument("--judge", default="Claude Opus 5.5, вслепую")
    ap.add_argument("--recommend", help="model to mark as the choice in the report")
    a = ap.parse_args()
    asyncio.run(write_packets(a.run) if a.step == "packets" else finalize(a.run, a.judge, a.recommend))


if __name__ == "__main__":
    main()
