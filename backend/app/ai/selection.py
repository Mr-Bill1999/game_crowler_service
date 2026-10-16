"""Which player reviews the model reads as text, and how the rest still counts.

Short reviews ("masterpiece", "worst game ever") carry a score but almost no information, and most of them are
0 or 10. They are not sent as text; instead every summary gets the score distribution of all reviews, so the
proportions of opinion stay right. Long reviews are deduplicated and, above a cap, sampled per score band in the
same proportions as the full set.
"""

import random
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from app.models import Review

SUBSTANTIVE_WORDS = 50
SAMPLE_CAP = 300
# Each audience on Metacritic's own thresholds: players 0–4 / 5–7 / 8–10 (as in its positive/mixed/negative counts),
# critics 0–49 / 50–74 / 75–100 (its red / yellow / green). Scaling the player bands to critics put 75–79 in "mixed".
BANDS = (("негативные 0–4", 0, 4.99), ("смешанные 5–7", 5, 7.99), ("позитивные 8–10", 8, 10))
CRITIC_BANDS = (("негативные 0–49", 0, 49.99), ("смешанные 50–74", 50, 74.99), ("позитивные 75–100", 75, 100))


def word_count(text: str | None) -> int:
    return len(re.findall(r"\w+", text or ""))


def bands_for(scale: float = 1.0) -> tuple:
    """Critic scores (0–100) come with scale=0.1, player scores (0–10) with 1."""
    return CRITIC_BANDS if scale != 1.0 else BANDS


def band(score: float | None, scale: float = 1.0) -> str | None:
    if score is None:
        return None
    return next((name for name, lo, hi in bands_for(scale) if lo <= score <= hi), None)


def scale_for(kind: str) -> float:
    return 0.1 if kind == "critic" else 1.0


def substantive(reviews: Sequence[Review], min_words: int = SUBSTANTIVE_WORDS) -> list[Review]:
    """Long enough to say something, first copy of any duplicated text."""
    seen: set[str] = set()
    out = []
    for r in reviews:
        key = re.sub(r"\W+", " ", (r.quote or "").lower()).strip()
        if word_count(r.quote) < min_words or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def stratified_sample(reviews: Sequence[Review], population: Sequence[Review], cap: int = SAMPLE_CAP,
                      seed: int = 0, scale: float = 1.0) -> list[Review]:
    """At most `cap` reviews, split across score bands in the proportions of `population` (all reviews).
    Inside a band the longest half is kept first, the rest is a fixed random draw."""
    if len(reviews) <= cap:
        return list(reviews)
    share = Counter(band(r.score, scale) for r in population if r.score is not None)
    total = sum(share.values()) or 1
    by_band: dict[str | None, list[Review]] = defaultdict(list)
    for r in reviews:
        by_band[band(r.score, scale)].append(r)
    rng = random.Random(seed)
    picked: list[Review] = []
    for name, _, _ in bands_for(scale):
        pool = sorted(by_band.get(name, []), key=lambda r: -word_count(r.quote))
        want = min(len(pool), round(cap * share.get(name, 0) / total))
        longest, rest = pool[: want // 2], pool[want // 2:]
        picked += longest + rng.sample(rest, min(len(rest), want - len(longest)))
    return sorted(picked, key=lambda r: (r.date is None, r.date), reverse=True)


def score_stats(reviews: Sequence[Review], metacritic: dict | None = None) -> str:
    """Score distribution of every player review, plus Metacritic's own counts when known (they include ratings
    without text)."""
    scored = [r for r in reviews if r.score is not None]
    n = len(scored) or 1
    counts = Counter(band(r.score) for r in scored)
    lines = [f"Распределение оценок всех {len(scored)} отзывов игроков с текстом (включая короткие, которых нет ниже):"]
    lines += [f"- {name}: {counts.get(name, 0)} ({round(100 * counts.get(name, 0) / n)}%)" for name, _, _ in BANDS]
    if metacritic and metacritic.get("reviewCount"):
        mc = metacritic
        t = mc["reviewCount"]
        lines.append(f"Все оценки Metacritic, включая оценки без текста: {t}; позитивные {mc.get('positiveCount', 0)} "
                     f"({round(100 * mc.get('positiveCount', 0) / t)}%), смешанные {mc.get('neutralCount', 0)}, "
                     f"негативные {mc.get('negativeCount', 0)}.")
    lines.append("Передавай пропорции мнений по этому распределению, а не по числу отзывов в выборке ниже. "
                 "Других процентов не приводи.")
    return "\n".join(lines)


def critic_stats(reviews: Sequence[Review], metascore: int | None) -> str:
    scored = [r for r in reviews if r.score is not None]
    n = len(scored) or 1
    counts = Counter(band(r.score, 0.1) for r in scored)
    head = f"Metascore: {metascore if metascore is not None else 'нет'}. Распределение оценок {len(scored)} рецензий"
    lines = [f"{head} (границы Metacritic):"]
    lines += [f"- {name}: {counts.get(name, 0)} ({round(100 * counts.get(name, 0) / n)}%)" for name, _, _ in CRITIC_BANDS]
    lines.append("Других процентов не приводи.")
    return "\n".join(lines)
