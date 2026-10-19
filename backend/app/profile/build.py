"""The profile: the model condenses a game's evidence into the six factors. Every point names the evidence it
stands on, and evidence ids the model invents are dropped. A factor with little evidence says so instead of
guessing: that is what the fullness indicator under the profile is for."""

from app.profile.fullness import FACTORS

SYSTEM = """Ты аналитик игровой индустрии. Тебе дают доказательства об одной игре, собранные из отзывов игроков,
рецензий критиков, страницы магазина и новостей разработчика. Составь профиль игры по шести факторам.

Правила:
- Опирайся только на доказательства. Каждый пункт ссылается на номера доказательств E…, на которых стоит.
- Пиши конкретно: называй механики, решения и приёмы этой игры. Не пиши того, что верно для любой игры жанра.
- Слова разработчика (помечены «заявление») — это намерение, а не подтверждение. Если игроки или критики
  говорят иначе, верь им и отметь расхождение в факторе 5.
- Фактор 1: 3–5 элементов, без которых игра перестаёт быть собой.
- Фактор 5: где сильная сторона отталкивает и для кого опыт перестаёт работать; пиши, для кого.
- Фактор 6: отличие от названных в доказательствах альтернатив и кому оно важно. Если альтернатив не названо,
  так и скажи.
- Если доказательств по фактору мало или они общие, summary: «Данных недостаточно», points пустые,
  confidence: low. Это лучше, чем догадка.
- summary — до трёх предложений. points — до пяти, каждый до 30 слов. Всё на русском."""

SCHEMA = {"type": "object", "properties": {
    "factors": {"type": "array", "items": {"type": "object", "properties": {
        "factor": {"type": "integer", "enum": list(FACTORS)},
        "summary": {"type": "string"},
        "points": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        }, "required": ["text", "evidence"]}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    }, "required": ["factor", "summary", "points", "confidence"]}},
}, "required": ["factors"]}

PER_FACTOR = 40  # evidence passed per factor: concrete and independent first


def pick(evidence: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for f in FACTORS:
        mine = [e for e in evidence if e["factor"] == f]
        mine.sort(key=lambda e: (not e["specific"], e["is_claim"], -e["id"]))
        out[f] = mine[:PER_FACTOR]
    return out


def evidence_line(e: dict) -> str:
    tags = [e["source"], {"plus": "+", "minus": "−", "neutral": "·"}[e["polarity"]]]
    if e["is_claim"]:
        tags.append("заявление")
    if e.get("who"):
        tags.append(f"для: {e['who']}")
    return f"E{e['id']} ({', '.join(tags)}) {e['claim']}"


def prompt(title: str, genres: list[str], chosen: dict[int, list[dict]], scores: dict) -> str:
    parts = [f"Игра: {title}", f"Жанры Metacritic: {', '.join(genres) or 'нет'}", ""]
    for f, name in FACTORS.items():
        parts.append(f"## {f}. {name} (наполненность {round(100 * scores[f]['score'])} из 100)")
        parts += [evidence_line(e) for e in chosen[f]] or ["(доказательств нет)"]
        parts.append("")
    return "\n".join(parts)


def clean(data: dict, known: set[int]) -> tuple[dict, int]:
    """Points keep only evidence that exists; a point left with none is dropped. Returns the payload by factor."""
    dropped, out = 0, {}
    for item in data.get("factors") or []:
        f = item.get("factor")
        if f not in FACTORS:
            continue
        points = []
        for p in item.get("points") or []:
            ids = [int(x.lstrip("Ee")) for x in p.get("evidence") or [] if x.lstrip("Ee").isdigit()]
            ok = [i for i in dict.fromkeys(ids) if i in known]
            dropped += len(ids) - len(ok)
            if ok and (p.get("text") or "").strip():
                points.append({"text": p["text"].strip(), "evidence": ok})
        out[str(f)] = {"summary": (item.get("summary") or "").strip(), "points": points,
                       "confidence": item.get("confidence") if item.get("confidence") in ("high", "medium", "low")
                       else "low"}
    return out, dropped


async def build(provider, title: str, genres: list[str], evidence: list[dict], scores: dict, *, model: str,
                thinking_level: str | None):
    chosen = pick(evidence)
    known = {e["id"] for es in chosen.values() for e in es}
    res = await provider.generate_json(system=SYSTEM, prompt=prompt(title, genres, chosen, scores), schema=SCHEMA,
                                       model=model, thinking_level=thinking_level)
    payload, dropped = clean(res.data, known)
    return payload, dropped, res
