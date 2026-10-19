"""Evidence: the model reads new documents of one source and returns claims per factor with the documents behind
each. Every document is read once; references the model makes up are dropped here, not trusted."""

from dataclasses import dataclass, field

from app.profile.fullness import FACTORS

FACTOR_QUESTIONS = {
    1: "ключевая идентичность: роль игрока и основной опыт; что обязано сохраниться, чтобы игра оставалась собой",
    2: "художественный крючок: визуальный язык, звук, повествование, необычное сочетание, делающее игру узнаваемой",
    3: "геймдизайнерское ядро: что игрок делает постоянно, какие решения принимает, как устроены риск и награда",
    4: "эмоциональное предложение: какие переживания создаёт игра и за счёт каких решений",
    5: "противоречия и ограничения: где сильная сторона отталкивает, для кого опыт перестаёт работать",
    6: "рыночная дифференциация: чем игра отличается от ближайших альтернатив и кому это отличие важно",
}
assert FACTOR_QUESTIONS.keys() == FACTORS.keys()

SYSTEM = """Ты аналитик игровой индустрии. Тебе дают тексты об одной игре из одного источника. Извлеки из них
доказательства по шести факторам профиля игры:
""" + "\n".join(f"{k}. {v}" for k, v in FACTOR_QUESTIONS.items()) + """

Правила:
- Только то, что прямо следует из текстов. Ничего не додумывай из знаний об игре или жанре.
- Каждое доказательство — одно короткое утверждение на русском (до 25 слов) и номера документов из квадратных
  скобок, на которых оно стоит. Указывай только те документы, где это действительно сказано.
- specific: true, только если утверждение называет конкретную механику, решение, приём или деталь этой игры.
  Общие слова («атмосферная», «затягивает», «красивая графика», «интересный геймплей») — specific: false.
  Для фактора 4 эмоция конкретна, если названо решение игры, которое её вызывает («напряжение от того, что
  сохранения только у костров»), и не конкретна без него («очень напряжённая»).
- polarity: plus — сильная сторона, minus — слабость или отторжение, neutral — описание.
- who: для какого игрока это верно или не верно, если тексты это говорят («новички», «фанаты жанра», «наигравшие
  50+ часов»), иначе пусто.
- Для фактора 6 нужны сравнения с другими играми или явное отличие от жанра.
- Текст магазина и новости разработчика — заявления разработчика, а не подтверждение: помечай source_is_claim: true.
- Если по фактору в текстах ничего нет, ничего не пиши. Лучше пусто, чем общие слова."""

SCHEMA = {"type": "object", "properties": {"evidence": {"type": "array", "items": {"type": "object", "properties": {
    "factor": {"type": "integer", "enum": list(FACTORS)},
    "claim": {"type": "string"},
    "docs": {"type": "array", "items": {"type": "integer"}},
    "specific": {"type": "boolean"},
    "polarity": {"type": "string", "enum": ["plus", "minus", "neutral"]},
    "who": {"type": "string"},
    "source_is_claim": {"type": "boolean"},
}, "required": ["factor", "claim", "docs", "specific", "polarity", "source_is_claim"]}}}, "required": ["evidence"]}

BATCH_CHARS = 60_000  # ~15k tokens of documents per call: long enough to compare, short enough to cite precisely
DEVELOPER_SOURCES = {"steam_store", "steam_news"}


@dataclass
class Doc:
    id: int
    kind: str
    text: str
    meta: dict


@dataclass
class Extracted:
    evidence: list[dict] = field(default_factory=list)
    calls: list[tuple[int, int]] = field(default_factory=list)  # tokens in, out per call
    dropped_refs: int = 0


def doc_line(i: int, d: Doc) -> str:
    m = d.meta or {}
    tags = []
    if "voted_up" in m:
        tags.append(("рекомендует" if m["voted_up"] else "не рекомендует") + f", {m.get('hours', 0)} ч в игре")
    if m.get("score") is not None:
        tags.append(f"оценка {m['score']}")
    if m.get("publication"):
        tags.append(m["publication"])
    return f"[{i}] {d.kind}" + (f" ({'; '.join(tags)})" if tags else "") + f"\n{d.text}"


def batches(docs: list[Doc]) -> list[list[tuple[int, Doc]]]:
    out, cur, size = [], [], 0
    for i, d in enumerate(docs):
        n = len(d.text) + 80
        if cur and size + n > BATCH_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append((i, d))
        size += n
    if cur:
        out.append(cur)
    return out


def accept(item: dict, batch: list[tuple[int, Doc]], source: str) -> tuple[dict | None, int]:
    """The evidence row to keep, with document ids instead of the prompt's numbers, and how many refs were bad."""
    by_number = {i: d.id for i, d in batch}
    refs = [by_number[n] for n in dict.fromkeys(item.get("docs") or []) if n in by_number]
    bad = len(set(item.get("docs") or [])) - len(refs)
    claim = (item.get("claim") or "").strip()
    if not refs or not claim or item.get("factor") not in FACTORS:
        return None, bad
    return {"factor": item["factor"], "claim": claim[:600], "document_ids": refs, "specific": bool(item["specific"]),
            "polarity": item.get("polarity") if item.get("polarity") in ("plus", "minus", "neutral") else "neutral",
            "who": (item.get("who") or "").strip()[:300] or None,
            "is_claim": bool(item.get("source_is_claim")) or source in DEVELOPER_SOURCES}, bad


async def extract(provider, title: str, source: str, docs: list[Doc], *, model: str,
                  thinking_level: str | None) -> Extracted:
    out = Extracted()
    for batch in batches(docs):
        prompt = f"Игра: {title}\nИсточник: {source}\n\nДокументы:\n\n" + "\n\n".join(doc_line(i, d) for i, d in batch)
        res = await provider.generate_json(system=SYSTEM, prompt=prompt, schema=SCHEMA, model=model,
                                           thinking_level=thinking_level)
        out.calls.append((res.tokens_in, res.tokens_out))
        for item in res.data.get("evidence") or []:
            row, bad = accept(item, batch, source)
            out.dropped_refs += bad
            if row:
                out.evidence.append(row)
    return out
