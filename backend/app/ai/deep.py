"""Deep analyze: Gemini watches a few minutes of a public let's play by its URL and reports what the store page
does not say. One request carries all clips; timecodes come back relative to a clip and are made absolute here."""

from google.genai import types

from app.ai.base import LLMResult
from app.ai.gemini import GeminiProvider
from app.models import Game
from app.sources.youtube import Clip, Video


def _list(n: int, item: dict | None = None) -> dict:
    return {"type": "array", "maxItems": n, "items": item or {"type": "string"}}


DEEP_SCHEMA = {
    "type": "object",
    "required": ["matches_game", "summary", "facts", "mechanics_shown", "author_liked", "author_disliked",
                 "tech_issues", "has_commentary"],
    "properties": {
        "matches_game": {"type": "boolean"},
        "summary": {"type": "string"},
        "facts": _list(8, {"type": "object", "required": ["text", "clip", "at"], "properties": {
            "text": {"type": "string"},
            "clip": {"type": "integer"},
            "at": {"type": "string"},
        }}),
        "mechanics_shown": _list(8),
        "author_liked": _list(4),
        "author_disliked": _list(4),
        "tech_issues": _list(4),
        "has_commentary": {"type": "boolean"},
    },
}

DEEP_SYSTEM = """Ты аналитик игровой индустрии. Тебе показывают несколько отрезков летсплея игры. Пиши по-русски,
конкретно, только о том, что видно и слышно в отрезках; ничего не додумывай.

matches_game — это действительно игровой процесс этой игры (а не другой игры, трейлер или разговоры без игры).
summary — 2-3 предложения: что показывают отрезки.
facts — факты об игре, которых нет в описании магазина: устройство систем, структура уровней, интерфейс, сложность,
  длина миссий. Для каждого: clip — номер отрезка (с 1), at — момент во всём видео, как в подписи отрезка
  (ч:мм:сс или мм:сс): внутри отрезка 2 с 20:15 по 25:15 это, например, 22:40.
mechanics_shown — механики, которые реально показаны.
author_liked / author_disliked — что автор хвалит и ругает, пересказ по-русски, без цитат на других языках;
  пусто, если он молчит или только восклицает.
tech_issues — заметные технические проблемы: просадки кадров, баги, подгрузки. Пусто, если их не видно.
has_commentary — автор комментирует голосом."""


def clock(s: int) -> str:
    h, rest = divmod(max(0, s), 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def deep_parts(game: Game, video: Video, clips: list[Clip], fps: float) -> list[types.Part]:
    desc = " ".join((game.description or "").split())[:1500] or "—"
    parts = [types.Part(text=(f"Игра: {game.title}\nЖанры: {', '.join(game.genres) or '—'}\n"
                              f"Описание из магазина: {desc}\n\nВидео: «{video.title}», канал {video.channel}."))]
    for n, c in enumerate(clips, 1):
        label = f" (глава «{c.label}»)" if c.label else ""
        parts.append(types.Part(text=f"Отрезок {n}: с {clock(c.start_s)} по {clock(c.end_s)}{label}."))
        parts.append(types.Part(file_data=types.FileData(file_uri=video.url),
                                video_metadata=types.VideoMetadata(start_offset=f"{c.start_s}s",
                                                                   end_offset=f"{c.end_s}s", fps=fps)))
    parts.append(types.Part(text="Разбери отрезки по инструкции."))
    return parts


def absolute_facts(data: dict, video: Video, clips: list[Clip]) -> list[dict]:
    """Timecodes → seconds into the video, with a link that opens YouTube at that moment. The model is asked for
    video time; one that still answers from the start of the clip is recognised and shifted."""
    out = []
    for f in data.get("facts", []):
        n = f.get("clip") or 1
        c = clips[min(max(n, 1), len(clips)) - 1]
        try:
            at = sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(f.get("at", "0")).split(":"))))
        except ValueError:
            at = c.start_s
        if c.start_s <= at <= c.end_s:
            t = at
        elif at <= c.length:
            t = c.start_s + at
        else:
            t = c.start_s
        out.append({"text": f.get("text", ""), "t": t, "at": clock(t), "url": f"{video.url}&t={t}s"})
    return sorted(out, key=lambda f: f["t"])  # in viewing order, whatever order the model listed them in


async def run_deep(provider: GeminiProvider, game: Game, video: Video, clips: list[Clip], *, model: str,
                   thinking_level: str | None, fps: float) -> LLMResult:
    return await provider.generate_json(system=DEEP_SYSTEM, prompt=deep_parts(game, video, clips, fps),
                                        schema=DEEP_SCHEMA, model=model, thinking_level=thinking_level,
                                        max_output_tokens=4096, media_resolution="low")
