"""MVP quality check of review summaries on answers we already have: no model is called.

Cases, by how many review texts the model read:
  db        the working database: summaries the worker made (lean first read), inputs rebuilt exactly
            (same reviews as of the analysis watermark, same seeded sample, same score statistics)
  compare   the model comparison of 15.10 (eval/results/20261015-0857): the production model on 12 big games,
            full read, the review texts it saw are kept in blind/sources (an earlier, English prompt)
  strategy  the strategy test (eval/results/strategy-v2): lean and incremental summaries of the three biggest games

  cd backend && .venv/bin/python -m eval.quality_mvp [--out eval/results/quality-mvp]
Writes cases.json (every check per summary), summary.json (by size bucket) and review_packet.md (for reading by hand).
"""

import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.ai.analyze import load_reviews
from app.ai.lifecycle import metacritic_user_summary, russian_share, texts_of
from app.ai.prompts import format_review
from app.ai.selection import critic_stats, scale_for, score_stats, stratified_sample
from app.core.db import SessionLocal
from app.models import Analysis, Game

RESULTS = Path(__file__).parent / "results"
COMPARE = RESULTS / "20261015-0857"
STRATEGY = RESULTS / "strategy-v2"
MODEL = "gemini-3.5-flash-lite"

TECH = re.compile(r"fps|кадр|частот|фрейм|вылет|краш|crash|баг|bug|глюк|glitch|зависа|фриз|freez|stutter|статтер|"
                  r"оптимиз|производител|perform|загруз|load|сервер|server|соединен|connect|разреш|resolut|"
                  r"текстур|textur|сохранен|save|управлен|control|интерфейс|\bui\b|звук|audio|sound|лаг|lag|"
                  r"drm|античит|anti-cheat|ошибк|error|disconnect|патч|patch|тормоз|просадк|техни|задержк|input|"
                  r"хитбокс|hitbox|pop-in|прорисовк|памят|ram\b|трассировк|ray.?trac|требовани|физик|physics|"
                  r"застрева|stuck|геометри|clipping|дерган|jitter|отзывчив|респонс|камер|camera|субтитр|subtit", re.IGNORECASE)
OVERCLAIM = re.compile(r"\bвсе критики|\bвсе игроки|единодушн|подавляющ|большинство|everyone|all critics|unanim", re.IGNORECASE)


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def bucket(n: int) -> str:
    return "1: <10" if n < 10 else "2: 10-49" if n < 50 else "3: 50-199" if n < 200 else "4: 200+"


def quote_cover(quote: str, texts: list[str]) -> float:
    """How much of the quote is one continuous piece of a single review: 1.0 verbatim, ~0.9 trimmed or lightly
    edited, well below means stitched, translated or made up."""
    import difflib
    nq = norm(quote)
    best = 0
    for t in texts:
        nt = norm(t)
        best = max(best, difflib.SequenceMatcher(None, nq, nt, autojunk=False)
                   .find_longest_match(0, len(nq), 0, len(nt)).size)
    return best / max(1, len(nq))


def quote_found(quote: str, corpus: str) -> bool:
    """A quote counts as verbatim when every fragment between ellipses is in some review (case and punctuation
    aside); fragments under 12 characters say nothing either way and are skipped."""
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if len(p) >= 12]
    return bool(parts) and all(p in corpus for p in parts)


def expected_consensus(kind: str, score: float | None) -> str | None:
    """Metacritic's own colour bands: critics 75+ / 50-74 / <50, players 7.5+ / 5-7.4 / <5."""
    if score is None:
        return None
    hi, lo = (75, 50) if kind == "critic" else (7.5, 5)
    return "positive" if score >= hi else "mixed" if score >= lo else "negative"


def check(data: dict, *, kind: str, texts: list[str], stats_text: str, score: float | None, n_texts: int,
          russian: bool) -> dict:
    corpus = norm("\n".join(texts))
    quotes = data.get("notable_quotes") or []
    found = [quote_found(q, corpus) for q in quotes]
    prose = " ".join([data.get("summary", ""), *(data.get("liked") or []), *(data.get("disliked") or [])])
    pcts = [int(x) for x in re.findall(r"(\d{1,3})\s?%", prose)]
    allowed = {int(x) for x in re.findall(r"(\d{1,3})\s?%", stats_text)}
    bad_pcts = [p for p in pcts if not any(abs(p - a) <= 1 for a in allowed)]
    tech = data.get("tech_issues") or []
    liked, disliked = data.get("liked") or [], data.get("disliked") or []
    clash = [(a, b) for a in liked for b in disliked
             if len(set(norm(a).split()) & set(norm(b).split())) / max(1, len(set(norm(a).split()) | set(norm(b).split()))) > 0.5]
    exp = expected_consensus(kind, score)
    return {
        "n_texts": n_texts, "bucket": bucket(n_texts),
        "quotes": len(quotes), "quotes_verbatim": sum(found),
        "unverified_quotes": [q for q, f in zip(quotes, found, strict=True) if not f],
        "quotes_broken": [q for q, f in zip(quotes, found, strict=True) if not f and quote_cover(q, texts) < 0.85],
        "percents": pcts, "percents_not_in_stats": bad_pcts,
        "russian": round(russian_share(data.get("summary", "")), 2) if russian else None,
        "limits_ok": len(liked) <= 6 and len(disliked) <= 6 and len(quotes) <= 4 and len(tech) <= 4,
        "liked": len(liked), "disliked": len(disliked),
        "tech": len(tech), "tech_not_technical": [t for t in tech if not TECH.search(t)],
        "consensus": data.get("consensus"), "expected": exp,
        "consensus_ok": None if exp is None else data.get("consensus") == exp,
        "consensus_far": None if exp is None else {data.get("consensus"), exp} == {"positive", "negative"},
        "overclaim_small_n": bool(n_texts < 5 and OVERCLAIM.search(prose)),
        "liked_disliked_clash": clash,
    }


async def db_cases() -> list[dict]:
    out = []
    async with SessionLocal() as s:
        rows = (await s.execute(select(Analysis, Game).join(Game, Game.id == Analysis.game_id)
                                .where(Analysis.kind.in_(("critic", "user"))).order_by(Analysis.id))).all()
        for a, g in rows:
            reviews = [r for r in await load_reviews(s, g.id, a.kind)
                       if a.watermark is None or r.fetched_at is None or r.fetched_at <= a.watermark]
            pool = texts_of(a.kind, reviews)
            sample = stratified_sample(pool, reviews, seed=g.id, scale=scale_for(a.kind))
            user_mc = await metacritic_user_summary(s, g.slug) if a.kind == "user" else None
            stats = score_stats(reviews, user_mc) if a.kind == "user" else critic_stats(reviews, g.metascore)
            score = g.metascore if a.kind == "critic" else g.userscore
            out.append({"source": "db", "slug": g.slug, "kind": a.kind, "mode": a.mode, "data": a.payload,
                        "input": [format_review(r) for r in sample], "stats": stats,
                        **check(a.payload, kind=a.kind, texts=[r.quote or "" for r in sample], stats_text=stats,
                                score=score, n_texts=len(pool), russian=True)})
    return out


def compare_cases() -> list[dict]:
    out = []
    for f in sorted((COMPARE / "outputs" / MODEL).glob("*.json")):
        d = json.loads(f.read_text())
        for kind in ("critic", "user"):
            t = d["tasks"].get(kind) or {}
            src = COMPARE / "blind" / "sources" / f"{d['slug']}.{kind}.txt"
            if not t.get("data") or not src.exists():
                continue
            lines = [ln for ln in src.read_text().splitlines() if ln.startswith("- [")]
            texts = [ln.split("): ", 1)[-1] for ln in lines]
            scores = [float(m.group(1)) for ln in lines if (m := re.match(r"- \[(\d+(?:\.\d+)?)", ln))]
            score = (sum(scores) / len(scores)) if scores else None
            out.append({"source": "compare", "slug": d["slug"], "kind": kind, "mode": "full", "data": t["data"],
                        "input": lines[:40], "stats": "",
                        **check(t["data"], kind=kind, texts=texts, stats_text="", score=score,
                                n_texts=len(lines), russian=False)})
    return out


def strategy_cases() -> list[dict]:
    out = []
    for f in sorted((STRATEGY / "outputs").glob("*.json")):
        slug, mode = f.stem.split(".")
        if mode == "full":
            continue  # the full read is the comparison run, counted there
        d = json.loads(f.read_text())
        packet = (STRATEGY / "packets" / f"{slug}.md").read_text()
        stats = packet.split("## Источник")[0]
        src = COMPARE / "blind" / "sources" / f"{slug}.user.txt"
        lines = [ln for ln in src.read_text().splitlines() if ln.startswith("- [")] if src.exists() else []
        m = re.search(r"Userscore: (\d+(?:\.\d+)?)", packet)
        out.append({"source": "strategy", "slug": slug, "kind": "user", "mode": mode, "data": d["data"],
                    "input": [], "stats": stats,
                    **check(d["data"], kind="user", texts=[ln.split("): ", 1)[-1] for ln in lines], stats_text=stats,
                            score=float(m.group(1)) if m else None, n_texts=d.get("substantive") or len(lines),
                            russian=True)})
    return out


def aggregate(cases: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        groups[f"{c['source']} | {c['kind']} | {c['bucket']}"].append(c)
        groups[f"ALL | {c['kind']}"].append(c)
    out = {}
    for k, cs in sorted(groups.items()):
        q = sum(c["quotes"] for c in cs)
        tech = sum(c["tech"] for c in cs)
        cons = [c for c in cs if c["consensus_ok"] is not None]
        rus = [c["russian"] for c in cs if c["russian"] is not None]
        out[k] = {
            "summaries": len(cs),
            "quotes_verbatim": f"{sum(c['quotes_verbatim'] for c in cs)}/{q}",
            "quotes_broken": sum(len(c["quotes_broken"]) for c in cs),
            "percent_claims": sum(len(c["percents"]) for c in cs),
            "percents_not_in_stats": sum(len(c["percents_not_in_stats"]) for c in cs),
            "russian_min": min(rus) if rus else None,
            "limits_ok": f"{sum(c['limits_ok'] for c in cs)}/{len(cs)}",
            "consensus_match": f"{sum(c['consensus_ok'] for c in cons)}/{len(cons)}",
            "consensus_opposite": sum(bool(c["consensus_far"]) for c in cons),
            "tech_not_technical": f"{sum(len(c['tech_not_technical']) for c in cs)}/{tech}",
            "overclaim_small_n": sum(c["overclaim_small_n"] for c in cs),
            "liked_disliked_clash": sum(len(c["liked_disliked_clash"]) for c in cs),
        }
    return out


def packet(cases: list[dict], per_bucket: int = 2) -> str:
    """Two summaries per source, kind and size, with what the model read, for reading by hand."""
    seen: dict[str, int] = defaultdict(int)
    parts = []
    for c in sorted(cases, key=lambda c: (c["source"], c["kind"], c["bucket"], -c["n_texts"])):
        k = f"{c['source']}|{c['kind']}|{c['bucket']}"
        if seen[k] >= per_bucket or c["source"] == "strategy":
            continue
        seen[k] += 1
        parts.append(f"### {c['slug']} — {c['kind']}, {c['source']}, {c['n_texts']} текстов\n")
        if c["stats"]:
            parts.append(f"Статистика для модели:\n```\n{c['stats'].strip()[:900]}\n```\n")
        parts.append("Что прочитала модель (до 40):\n" + "\n".join(x[:400] for x in c["input"][:40]) + "\n")
        parts.append("Ответ:\n```json\n" + json.dumps(c["data"], ensure_ascii=False, indent=1) + "\n```\n")
    return "\n".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=RESULTS / "quality-mvp")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cases = await db_cases() + compare_cases() + strategy_cases()
    (a.out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1))
    summary = aggregate(cases)
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    (a.out / "review_packet.md").write_text(packet(cases))
    for k, v in summary.items():
        print(f"{k:32} {v}")


if __name__ == "__main__":
    asyncio.run(main())
