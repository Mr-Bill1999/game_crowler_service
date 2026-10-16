"""Light essence: what a game is about, from its store description and genres alone, so every game has one,
not only those with enough reviews. Used for "similar games": short facet texts without proper names (names would
pull similarity towards titles and franchises) plus tags from a closed vocabulary.

Picked by the blind similarity experiment, backend/eval/results/similarity-v1/report.md.
"""

import hashlib
import re

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


# The finisher: what the model left out but the texts say outright. Each tag is added when one of its words occurs
# in the Metacritic genres, the essence's genre and gameplay, or the store description. Tags are only added, never
# removed: the model's reading stands, the texts fill its gaps (Castlevania: "метроидвания" in its own genre line,
# no metroidvania tag).
TAG_WORDS: dict[str, tuple[str, ...]] = {
    "mechanics:metroidvania": ("metroidvania", "метроидван"),
    "mechanics:roguelike-runs": ("roguelike", "roguelite", "rogue-like", "rogue-lite", "рогалик", "роглайк", "рогалайт"),
    "mechanics:visual-novel": ("visual novel", "визуальная новелла", "визуальной новелл"),
    "mechanics:tower-defense": ("tower defense", "tower defence", "tower-defense"),
    "mechanics:deck-building": ("deckbuild", "deck-build", "deck build", "колодострое"),
    "mechanics:card-battle": ("card battle", "карточн"),
    "mechanics:soulslike-combat": ("soulslike", "souls-like", "соулслайк", "соулс-лайк"),
    "mechanics:bullet-hell": ("bullet hell", "bullet-hell", "bullet heaven", "буллет-хелл"),
    "mechanics:rhythm": ("rhythm", "ритм-игр", "ритмическ"),
    "mechanics:racing": ("racing", "racer", "гонк", "гоночн"),
    "mechanics:fishing": ("fishing", "рыбалк"),
    "mechanics:farming": ("farming", "фермерск", "ферма"),
    "mechanics:city-building": ("city build", "city-build", "градостро"),
    "mechanics:point-and-click": ("point-and-click", "point and click", "point & click"),
    "mechanics:hidden-objects": ("hidden object", "hidden-object", "поиск предметов"),
    "mechanics:open-world": ("open world", "open-world", "открытый мир", "открытом мире", "открытого мира"),
    "mechanics:stealth": ("stealth", "стелс"),
    "mechanics:platforming": ("platformer", "платформер"),
    "mechanics:turn-based-tactics": ("turn-based tactic", "tactical rpg", "tactics rpg", "пошаговая тактик"),
    "mechanics:real-time-strategy": ("real-time strategy", "стратегия в реальном времени"),
    "mechanics:business-tycoon": ("tycoon", "тайкун"),
    "mechanics:fighting": ("fighting game", "fighter", "файтинг", "2d fighting", "3d fighting"),
    "mechanics:party-minigames": ("party game", "party-game", "игра для вечеринок"),
    "mechanics:idle-incremental": ("idle game", "incremental", "инкрементал", "кликер"),
    "mechanics:monster-collecting": ("monster collect", "creature collect", "monster-taming", "коллекционирование монстр"),
    "mechanics:flight": ("flight sim", "flight combat", "авиасим"),
}
FINISH_CAP = 8  # the model gives at most 6 mechanics; the finisher may add up to two more


def _norm_words(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"(\d)[,.](\d{3})\b", r"\1\2", s)  # 40,000 -> 40000
    s = re.sub(r"\b(\d+)k\b", r"\g<1>000", s)  # 40k -> 40000
    return " " + re.sub(r"[^\w]+", " ", s).strip() + " "


def series_in_title(title: str, known: list[str]) -> str | None:
    """The longest known series whose name occurs in the title as whole words: "Disney/Pixar Toy Story: Retro Roundup!"
    is in "Toy Story". Titles only: store texts name other franchises too ("from the makers of Spider-Man")."""
    t = _norm_words(title)
    hits = [s for s in known if len(s.strip()) >= 4 and _norm_words(s).strip() and _norm_words(s) in t]
    return max(hits, key=len) if hits else None


def finish_essence(e: dict, *, title: str, genres: list[str], description: str, known_series: list[str]) -> dict:
    """Series and tags the texts state outright; changes are listed in e["_finished"]."""
    e = dict(e)
    added: list[str] = []
    own = (e.get("series") or "").strip()
    # a known series named in the title wins over an empty or longer variant ("Disney/Pixar Toy Story" -> "Toy Story")
    found = series_in_title(title, [s for s in known_series if s])
    if found and (not own or (_norm_words(found) in _norm_words(own) and _norm_words(found) != _norm_words(own))):
        e["series"] = found
        added.append(f"series:{found}")
    text = _norm_words(" ".join([*(genres or []), e.get("genre", ""), e.get("gameplay", ""), description or ""]))
    for tag, words in TAG_WORDS.items():
        group, value = tag.split(":", 1)
        current = e.get(group) or []
        if value in current or len(current) >= FINISH_CAP:
            continue
        if any(" " + _norm_words(w).strip() in text for w in words):  # from a word's start: not "embracer"
            e[group] = [*current, value]
            added.append(tag)
    if added:
        e["_finished"] = sorted(set(e.get("_finished", [])) | set(added))
    return e
