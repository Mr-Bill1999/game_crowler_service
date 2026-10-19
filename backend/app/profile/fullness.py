"""Fullness: how much concrete, corroborated evidence there is for each of the six factors. No model calls.

The formula is the one the 20-game test was scored with (docs/game-profile.md):
  factor = 0.75 * coverage + 0.25 * corroboration
  coverage       concrete claims, up to TARGET; the developer's own words (store page, news) count a third
  corroboration  the factor is seen by two kinds of sources (players, critics, developer, video)
  factor 5       counts only weaknesses and claims about who the game stops working for
  fullness       the mean of the six factors, 0..100
"""

from dataclasses import dataclass

FACTORS = {
    1: "Ключевая идентичность",
    2: "Художественный крючок",
    3: "Геймдизайнерское ядро",
    4: "Эмоциональное предложение",
    5: "Противоречия и ограничения",
    6: "Рыночная дифференциация",
}
TARGET = 3
KIND = {"mc": "critics_players", "steam_store": "developer", "steam_news": "developer", "steam_reviews": "players",
        "critic_full": "critics", "youtube": "video"}
BANDS = [(80, "rich", "богато"), (60, "enough", "достаточно"), (30, "partial", "частично"), (0, "thin", "скудно")]
STEAM_REVIEWS_ENOUGH = 20  # in the test, 20+ Steam reviews split passed and failed games exactly


@dataclass
class Point:
    """What fullness needs to know about one piece of evidence."""

    factor: int
    source: str
    specific: bool
    polarity: str
    who: str | None
    is_claim: bool


def factor_score(points: list[Point], factor: int) -> dict:
    mine = [p for p in points if p.factor == factor and p.specific]
    independent = [p for p in mine if not p.is_claim]
    if factor == 5:
        count = (sum(p.polarity == "minus" for p in independent)
                 + 0.5 * sum(p.polarity != "minus" and bool(p.who) for p in independent))
    else:
        count = len(independent) + sum(p.is_claim for p in mine) / 3
    kinds = sorted({KIND.get(p.source, p.source) for p in independent})
    score = 0.75 * min(1.0, count / TARGET) + 0.25 * min(1.0, len(kinds) / 2)
    return {"score": round(score, 3), "points": round(count, 2), "kinds": kinds}


def band(fullness: int) -> tuple[str, str]:
    return next((code, label) for floor, code, label in BANDS if fullness >= floor)


def fullness(points: list[Point]) -> tuple[int, dict]:
    factors = {f: factor_score(points, f) for f in FACTORS}
    return round(100 * sum(x["score"] for x in factors.values()) / len(FACTORS)), factors


def missing(factors: dict, docs: dict[str, int], steam_linked: bool | None, critic_links: int) -> list[str]:
    """What would make the profile fuller, in the reader's words: the sources first, then the weak factors."""
    out = []
    if steam_linked is False:
        out.append("Игры нет в Steam: нет отзывов игроков с часами в игре")
    elif docs.get("steam_reviews", 0) < STEAM_REVIEWS_ENOUGH:
        out.append(f"Мало отзывов в Steam: {docs.get('steam_reviews', 0)} из {STEAM_REVIEWS_ENOUGH} нужных")
    if critic_links and not docs.get("critic_full"):
        out.append("Полные тексты рецензий недоступны")
    elif not critic_links:
        out.append("Нет рецензий критиков")
    weak = [FACTORS[int(f)] for f, x in factors.items() if x["score"] < 0.5]
    if weak:
        out.append("Мало данных: " + ", ".join(w.lower() for w in weak))
    return out
