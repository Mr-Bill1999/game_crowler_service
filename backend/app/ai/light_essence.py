"""Light essence: what a game is about, from its store description and genres alone, so every game has one,
not only those with enough reviews. Used for "similar games": short facet texts without proper names (names would
pull similarity towards titles and franchises) plus tags from a closed vocabulary.

Picked by the blind similarity experiment, backend/eval/results/similarity-v1/report.md.
"""

import hashlib

from app.models import Game

MIN_DESCRIPTION = 150  # shorter store texts rarely say how the game plays

# Closed vocabulary: the model may only pick from these, so tags of two games are comparable as-is.
MECHANICS = [
    "melee-combat", "shooting", "soulslike-combat", "hack-and-slash", "fighting", "bullet-hell", "stealth",
    "platforming", "precision-platforming", "exploration", "open-world", "metroidvania", "roguelike-runs",
    "puzzle-solving", "physics-puzzles", "hidden-objects", "point-and-click", "investigation", "story-choices",
    "dialogue-heavy", "visual-novel", "dating-relationships", "turn-based-combat", "turn-based-tactics",
    "real-time-strategy", "4x-grand-strategy", "tower-defense", "deck-building", "card-battle", "board-game",
    "auto-battler", "base-building", "city-building", "colony-management", "business-tycoon", "job-simulation",
    "farming", "cooking", "fishing", "life-sim", "crafting", "survival-needs", "resource-management", "loot",
    "character-progression", "party-management", "monster-collecting", "racing", "driving", "flight",
    "sports", "rhythm", "party-minigames", "trivia-quiz", "sandbox-building", "idle-incremental",
    "horror-chase", "vehicle-combat", "naval", "trading-economy",
]
PERSPECTIVE = ["first-person", "third-person", "top-down", "isometric", "side-view", "fixed-screen", "menu-text"]
MODES = ["single-player", "local-multiplayer", "online-coop", "online-pvp", "massively-multiplayer"]
SETTING = [
    "high-fantasy", "dark-fantasy", "sci-fi", "space", "cyberpunk", "post-apocalyptic", "contemporary",
    "historical", "military", "mythology", "supernatural-horror", "cartoon-whimsical", "real-world-sports",
    "nature-rural", "urban", "abstract", "superhero", "licensed-franchise",
]
TONE = ["dark", "tense", "serious", "epic", "melancholic", "cozy", "relaxing", "humorous", "whimsical", "competitive"]
TAG_GROUPS = {"mechanics": (MECHANICS, 2, 6), "perspective": (PERSPECTIVE, 1, 1), "modes": (MODES, 1, 3),
              "setting": (SETTING, 1, 3), "tone": (TONE, 1, 2)}
TAG_GROUP_WEIGHT = {"mechanics": 0.5, "setting": 0.2, "tone": 0.1, "perspective": 0.1, "modes": 0.1}


def _enum_list(values: list[str], lo: int, hi: int) -> dict:
    return {"type": "array", "minItems": lo, "maxItems": hi, "items": {"type": "string", "enum": values}}


LIGHT_ESSENCE_SCHEMA = {
    "type": "object",
    "required": ["genre", "gameplay", "world", *TAG_GROUPS, "series", "comparable_to", "confidence"],
    "properties": {
        "genre": {"type": "string"},
        "gameplay": {"type": "string"},
        "world": {"type": "string"},
        **{k: _enum_list(v, lo, hi) for k, (v, lo, hi) in TAG_GROUPS.items()},
        "series": {"type": "string"},
        "comparable_to": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}

LIGHT_ESSENCE_SYSTEM = """Ты аналитик игровой индустрии. По описанию из магазина и жанрам опиши суть игры для поиска похожих.
Пиши по-русски, конкретно, без рекламных оборотов. Опирайся только на переданные данные, не выдумывай.

genre — жанр в 2-4 словах.
gameplay — что игрок делает минуту за минутой: основной цикл, ключевые механики, структура прохождения, режимы.
  До 450 символов. Без названий игр, студий, персонажей и мест — только действия и системы.
world — сеттинг, атмосфера и тон. До 300 символов. Тоже без собственных имён.
mechanics, perspective, modes, setting, tone — только значения из списков схемы, самые точные.
series — франшиза, если игра в неё входит (как она известна игрокам), иначе пустая строка.
comparable_to — до 4 известных игр, похожих по механикам; пусто, если не уверен.
confidence — хватило ли описания, чтобы понять игровой процесс."""


def essence_prompt(g: Game) -> str:
    return (f"Игра: {g.title}\nЖанры Metacritic: {', '.join(g.genres) or '—'}\nРазработчик: {g.developer or '—'}\n"
            f"Описание:\n{' '.join((g.description or '').split())[:3000]}")


def texts(e: dict, title: str) -> dict[str, str]:
    tags = "; ".join(t for k in TAG_GROUPS for t in e[k])
    return {
        "whole": (f"{title}. Жанр: {e['genre']}. {e['gameplay']} Мир: {e['world']} Теги: {tags}. "
                  f"Серия: {e['series'] or '—'}. Похожа на: {', '.join(e['comparable_to']) or '—'}."),
        "gameplay": f"Жанр: {e['genre']}. {e['gameplay']}",
        "world": e["world"],
    }


def clean_tags(e: dict) -> dict:
    """Keep only vocabulary values, deduplicated and capped: the schema enum is advisory on some paths."""
    for k, (allowed, _, hi) in TAG_GROUPS.items():
        e[k] = [t for t in dict.fromkeys(e.get(k) or []) if t in allowed][:hi]
    return e


def flat_tags(e: dict) -> list[str]:
    return [f"{k}:{t}" for k in TAG_GROUPS for t in e.get(k) or []]


def source_hash(g: Game) -> str:
    """The essence is rebuilt only when what it was made from changes."""
    src = "\n".join([g.title or "", ",".join(g.genres or []), " ".join((g.description or "").split())])
    return hashlib.sha256(src.encode()).hexdigest()
