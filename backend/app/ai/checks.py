"""Checks of a review summary against what the model read, and the fixes that need no model.

Found by the MVP quality check (docs/quality-mvp.md): a fast model misspells the game's title, tells the reader about
"our sample", quotes reviews almost but not quite, and now and then gives a percentage that was not in the data.
`problems` lists what is worth one more generation; `finish` repairs what can be repaired by itself.
"""

import difflib
import re
from dataclasses import dataclass

QUOTE_KEEP = 0.85  # a quote with less of itself found in one review is not a quote
TITLE_NEAR = 0.8


@dataclass
class Problem:
    kind: str  # title | sample | percent | language
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def prose_of(data: dict) -> str:
    return " ".join([data.get("summary", ""), *(data.get("liked") or []), *(data.get("disliked") or []),
                     *(data.get("tech_issues") or [])])


def title_variants(text: str, title: str) -> list[str]:
    """Spans of the text that look like the title but are not it: "Order of the Sinking State" for "...Star"."""
    words = re.findall(r"\S+", text)
    n = len(title.split())
    want = norm(title)
    if not want or n == 0:
        return []
    out = []
    for i in range(len(words) - n + 1):
        span = " ".join(words[i:i + n]).strip(".,;:!?«»\"'()")
        got = norm(span)
        if got and got != want and difflib.SequenceMatcher(None, got, want).ratio() >= TITLE_NEAR:
            out.append(span)
    return list(dict.fromkeys(out))


def problems(data: dict, *, title: str, stats: str, russian_share) -> list[Problem]:
    prose = prose_of(data)
    out = []
    if russian_share(data.get("summary", "")) < 0.5:
        out.append(Problem("language", "summary не на русском"))
    for v in title_variants(prose, title):
        out.append(Problem("title", f"«{v}» вместо «{title}»"))
    if re.search(r"выборк|отобранн|в нашей подборк", prose, re.IGNORECASE):
        out.append(Problem("sample", "упоминается выборка отзывов"))
    allowed = {int(x) for x in re.findall(r"(\d{1,3})\s?%", stats)}
    for p in {int(x) for x in re.findall(r"(\d{1,3})\s?%", prose)}:
        if not any(abs(p - a) <= 1 for a in allowed):
            out.append(Problem("percent", f"{p}% нет в переданной статистике"))
    return out


def retry_note(found: list[Problem], title: str) -> str:
    lines = {"title": f"Название игры пиши точно: «{title}».",
             "sample": "Не упоминай выборку и число прочитанных отзывов: пиши об отзывах игроков или критиков в целом.",
             "percent": "Приводи только проценты из переданной статистики; других процентов не давай.",
             "language": "Пиши summary и пункты по-русски."}
    asked = "\n".join(dict.fromkeys(f"- {lines[p.kind]}" for p in found))
    return f"\n\nВ прошлом ответе были ошибки ({'; '.join(map(str, found))}). Исправь:\n{asked}"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]


def snap_quote(quote: str, texts: list[str]) -> str | None:
    """The quote as the review actually says it, or None when no review says it.
    Verbatim stays; trimmed or lightly edited becomes the review's own sentences that match it best."""
    nq = norm(quote)
    if not nq:
        return None
    best_cover, best_text = 0.0, ""
    for t in texts:
        nt = norm(t)
        if nq in nt:
            return quote
        m = difflib.SequenceMatcher(None, nq, nt, autojunk=False).find_longest_match(0, len(nq), 0, len(nt))
        if m.size / len(nq) > best_cover:
            best_cover, best_text = m.size / len(nq), t
    if best_cover < QUOTE_KEEP:
        return None
    sents = _sentences(best_text)
    best, score = None, 0.0
    for i in range(len(sents)):
        for j in range(i + 1, min(len(sents), i + 4) + 1):
            span = " ".join(sents[i:j])
            r = difflib.SequenceMatcher(None, nq, norm(span), autojunk=False).ratio()
            if r > score:
                best, score = span, r
    return best if best and len(best) <= 400 else None


def finish(data: dict, *, title: str, texts: list[str]) -> dict:
    """Repairs that need no model: quotes snapped to the reviews (or dropped), the title spelled right."""
    data = dict(data)
    quotes = []
    for q in data.get("notable_quotes") or []:
        s = snap_quote(q, texts)
        if s and s not in quotes:
            quotes.append(s)
    data["notable_quotes"] = quotes
    for v in title_variants(prose_of(data), title):
        for field in ("summary",):
            data[field] = data.get(field, "").replace(v, title)
        for field in ("liked", "disliked", "tech_issues"):
            data[field] = [x.replace(v, title) for x in data.get(field) or []]
    return data
