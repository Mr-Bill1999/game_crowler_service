"""Analysis over a game's life: the first read samples substantive reviews, later runs only add what is new.

First read: critic reviews with a quote and player reviews of 50+ words, deduplicated, at most 300 sampled in
the score proportions of all reviews, plus a score-distribution block so short reviews still count.
Update: the previous summary plus reviews fetched after its watermark, merged under fixed list limits
(replace, never append). Validated in eval/strategy_test.py: same quality as reading everything, 2.5x cheaper
over a game's life than re-reading the sample on every trigger.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import checks
from app.ai.base import REVIEW_SUMMARY_SCHEMA, LLMProvider, LLMResult
from app.ai.prompts import SYSTEM, UPDATE_SYSTEM, format_review, lean_summary_prompt, update_prompt
from app.ai.selection import critic_stats, scale_for, score_stats, stratified_sample, substantive
from app.models import Analysis, Game, RawResponse, Review

MIN_TEXTS = {"critic": 3, "user": 5}


@dataclass
class KindRun:
    kind: str
    mode: str  # first | update | skip
    data: dict | None = None
    calls: list[LLMResult] = field(default_factory=list)
    read: int = 0  # reviews sent to the model in this run
    texts_seen: int = 0  # text reviews of this kind covered after this run
    watermark: datetime | None = None
    error: str | None = None

    @property
    def tokens_in(self) -> int:
        return sum(c.tokens_in for c in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(c.tokens_out for c in self.calls)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


def texts_of(kind: str, reviews: list[Review]) -> list[Review]:
    if kind == "critic":
        return [r for r in reviews if r.quote]
    return substantive(reviews)


def russian_share(text: str) -> float:
    letters = re.findall(r"[^\W\d_]", text or "")
    return sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / max(len(letters), 1)


async def latest_analyses(session: AsyncSession, game_id: int) -> dict[str, Analysis]:
    rows = (await session.execute(select(Analysis).where(Analysis.game_id == game_id)
                                  .order_by(Analysis.created_at.desc(), Analysis.id.desc()))).scalars()
    latest: dict[str, Analysis] = {}
    for a in rows:
        latest.setdefault(a.kind, a)
    return latest


async def metacritic_user_summary(session: AsyncSession, slug: str) -> dict | None:
    raw = await session.scalar(select(RawResponse.body).where(RawResponse.kind == "composer", RawResponse.key == slug))
    if not raw:
        return None
    comps = {c["meta"]["componentName"]: c.get("data") or {} for c in raw.get("components", [])}
    return (comps.get("user-score-summary") or {}).get("item")


async def _generate(provider: LLMProvider, *, system: str, prompt: str, model: str, thinking: str | None,
                    run: KindRun, title: str, stats: str, texts: list[str]) -> dict:
    """One call; one more when the answer has a fixable problem (wrong language, misspelled title, talk of "the
    sample", a percentage not in the data), then repairs that need no model: quotes snapped to the reviews.
    Whatever is still wrong is kept in "_checks" for monitoring."""
    r = await provider.generate_json(system=system, prompt=prompt, schema=REVIEW_SUMMARY_SCHEMA, model=model,
                                     thinking_level=thinking)
    run.calls.append(r)
    found = checks.problems(r.data, title=title, stats=stats, russian_share=russian_share)
    if found:
        r = await provider.generate_json(system=system, prompt=prompt + checks.retry_note(found, title),
                                         schema=REVIEW_SUMMARY_SCHEMA, model=model, thinking_level=thinking)
        run.calls.append(r)
    data = checks.finish(r.data, title=title, texts=texts)
    left = checks.problems(data, title=title, stats=stats, russian_share=russian_share)
    if left:
        data["_checks"] = [str(p) for p in left]
    return data


async def analyze_kind(provider: LLMProvider, g: Game, kind: str, reviews: list[Review], prev: Analysis | None,
                       model: str, thinking: str | None, user_mc: dict | None = None) -> KindRun:
    pool = texts_of(kind, reviews)
    scale = scale_for(kind)
    run = KindRun(kind, "skip", texts_seen=len(pool))
    run.watermark = max((r.fetched_at for r in reviews if r.fetched_at), default=None)
    try:
        if prev is None or prev.watermark is None:
            if len(pool) < MIN_TEXTS[kind]:
                return run
            sample = stratified_sample(pool, reviews, seed=g.id, scale=scale)
            stats = score_stats(reviews, user_mc) if kind == "user" else critic_stats(reviews, g.metascore)
            run.mode, run.read = "first", len(sample)
            run.data = await _generate(provider, system=SYSTEM, prompt=lean_summary_prompt(
                g, kind, [format_review(r) for r in sample], stats, len(pool)), model=model, thinking=thinking, run=run,
                title=g.title, stats=stats, texts=[r.quote or "" for r in sample])
            return run

        fresh = [r for r in reviews if r.fetched_at and r.fetched_at > prev.watermark]
        new = texts_of(kind, fresh)
        if not new:
            run.watermark = prev.watermark if run.watermark is None else max(run.watermark, prev.watermark)
            return run
        new = stratified_sample(new, fresh, seed=g.id + len(pool), scale=scale)
        stats = score_stats(reviews, user_mc) if kind == "user" else critic_stats(reviews, g.metascore)
        run.mode, run.read = "update", len(new)
        run.data = await _generate(provider, system=UPDATE_SYSTEM, prompt=update_prompt(
            g, kind, prev.payload, [format_review(r) for r in new], stats), model=model, thinking=thinking, run=run,
            title=g.title, stats=stats,
            texts=[r.quote or "" for r in new] + list(prev.payload.get("notable_quotes") or []))
    except Exception as e:  # noqa: BLE001  a failed kind must not lose the other one
        run.error = f"{type(e).__name__}: {e}"[:500]
        run.mode = "error"
    return run

