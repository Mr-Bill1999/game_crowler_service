"""Prompts for review summaries and the game essence. Output is Russian; quotes stay in the original."""

import json
from collections.abc import Sequence

from app.models import Game, Review

SYSTEM = """Ты аналитик игровой индустрии. Ты читаешь отзывы о видеоигре и сводишь их в короткий структурированный
разбор для людей, которые решают, стоит ли следить за игрой.

Правила:
- Опирайся только на переданные отзывы и данные об игре. Не добавляй фактов, которых в них нет.
- Пиши по-русски, ясно и конкретно. Цитаты в notable_quotes приводи дословно, на языке оригинала.
- Пункты «понравилось» и «не понравилось» должны быть конкретными для этой игры: механики, техническое
  состояние, сюжет, баланс, цена, длительность. Общие фразы вроде «хорошая графика» без деталей не нужны.
- Если о чём-то пишут один-два автора, а большинство иначе, отрази это как мнение меньшинства.
- Если отзывов мало или они противоречат друг другу, скажи об этом в summary.
- Цифры (проценты, оценки, часы, цены) приводи только те, что есть в переданных данных. Не округляй их по-своему
  и не придумывай разбивок, которых нет.
- summary — не больше 5 предложений. В списках — самое важное и частое, по одному пункту на тему: до 6 плюсов,
  до 6 минусов, до 4 цитат, до 4 технических проблем. В tech_issues только техническое состояние: баги,
  производительность, вылеты, серверы; навигация, баланс и дизайн туда не относятся."""

CHUNK_SYSTEM = SYSTEM + """

Сейчас ты видишь только часть отзывов. Сделай разбор этой части: позже части будут сведены вместе.
Указывай, насколько часто встречается мнение (например, «многие», «несколько авторов»)."""

REDUCE_SYSTEM = SYSTEM + """

На входе разборы нескольких частей одних и тех же отзывов. Сведи их в один разбор: объедини повторы,
сохрани пропорции мнений, оставь самые показательные цитаты."""

ESSENCE_SYSTEM = """Ты аналитик игровой индустрии. По описанию игры и разбору отзывов опиши суть игры: жанр, основной
игровой цикл, механики, сеттинг и чем она выделяется. Пиши по-русски, конкретно, без рекламных оборотов.
Опирайся только на переданные данные. comparable_to — 2-5 известных игр, на которые она похожа по механикам."""

# Roughly 50k tokens per chunk keeps every model, including Flash-Lite, well inside its comfort zone.
CHUNK_CHARS = 200_000


def game_header(g: Game) -> str:
    platforms = ", ".join(sorted(p.name for p in g.platforms))
    return (f"Игра: {g.title}\nРазработчик: {g.developer or '—'}\nЖанры: {', '.join(g.genres) or '—'}\n"
            f"Платформы: {platforms}\nДата релиза: {g.release_date or '—'}\n"
            f"Metascore: {g.metascore if g.metascore is not None else 'нет'} ({g.critic_count} рецензий), "
            f"Userscore: {g.userscore if g.userscore is not None else 'нет'} ({g.user_count} оценок)")


def format_review(r: Review) -> str:
    who = r.publication or r.author or "аноним"
    if r.kind == "critic" and r.author and r.publication:
        who = f"{r.publication}, {r.author}"
    score = "—" if r.score is None else (f"{r.score:g}/100" if r.kind == "critic" else f"{r.score:g}/10")
    spoiler = " [спойлер]" if r.spoiler else ""
    quote = " ".join((r.quote or "").split())
    return f"- [{score}] {who} ({r.platform or '—'}, {r.date or '—'}){spoiler}: {quote}"


def chunk_reviews(reviews: Sequence[Review], limit: int = CHUNK_CHARS) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    size = 0
    for line in (format_review(r) for r in reviews):
        if size + len(line) > limit and chunks[-1]:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line)
    return chunks


def summary_prompt(g: Game, kind: str, lines: list[str], part: tuple[int, int] | None = None) -> str:
    who = "критиков" if kind == "critic" else "игроков"
    head = f"{game_header(g)}\n\nОтзывы {who}"
    if part:
        head += f" (часть {part[0]} из {part[1]})"
    return f"{head}, {len(lines)} шт.:\n" + "\n".join(lines) + f"\n\nСделай разбор отзывов {who}."


def reduce_prompt(g: Game, kind: str, partials: list[dict]) -> str:
    who = "критиков" if kind == "critic" else "игроков"
    body = "\n\n".join(f"Разбор части {i + 1}:\n{json.dumps(p, ensure_ascii=False)}" for i, p in enumerate(partials))
    return f"{game_header(g)}\n\n{body}\n\nСведи разборы частей в один разбор отзывов {who}."


def essence_prompt(g: Game, critic: dict | None, user: dict | None) -> str:
    parts = [game_header(g), f"Описание:\n{g.description or '—'}"]
    if critic:
        parts.append(f"Разбор рецензий критиков:\n{json.dumps(critic, ensure_ascii=False)}")
    if user:
        parts.append(f"Разбор отзывов игроков:\n{json.dumps(user, ensure_ascii=False)}")
    return "\n\n".join(parts) + "\n\nОпиши суть игры."


UPDATE_SYSTEM = SYSTEM + """

Тебе дают текущий разбор отзывов (JSON) и новые отзывы, пришедшие после него. Верни новую версию разбора
того же размера — это замена, а не дополнение:
- Размер списков фиксирован лимитами. Новая тема попадает в список, только если она важнее или чаще самой
  слабой из текущих — тогда слабую убери. Похожие пункты объединяй в один.
- Если новые отзывы подтверждают пункт, оставь его как есть или уточни формулировку; не добавляй дубль.
- Цитаты: оставь до 4 самых показательных за всё время; новая цитата вытесняет старую, только если лучше её.
- summary перепиши целиком (не больше 5 предложений) под итоговое распределение оценок. Не пиши «новые отзывы
  продолжают…»: разбор описывает игру целиком, а не последнее обновление.
- Старые отзывы ты больше не увидишь, поэтому не выбрасывай устойчивые темы ради единичных новых упоминаний."""


def lean_summary_prompt(g: Game, kind: str, lines: list[str], stats: str, sampled_from: int) -> str:
    who = "критиков" if kind == "critic" else "игроков"
    note = (f"Ниже {len(lines)} содержательных отзывов {who} из {sampled_from} (короткие и дубли убраны"
            + (", выборка пропорциональна оценкам)" if len(lines) < sampled_from else ")"))
    return f"{game_header(g)}\n\n{stats}\n\n{note}:\n" + "\n".join(lines) + f"\n\nСделай разбор отзывов {who}."


def update_prompt(g: Game, kind: str, current: dict, lines: list[str], stats: str) -> str:
    who = "критиков" if kind == "critic" else "игроков"
    return (f"{game_header(g)}\n\n{stats}\n\nТекущий разбор отзывов {who}:\n{json.dumps(current, ensure_ascii=False)}\n\n"
            f"Новые отзывы {who}, {len(lines)} шт.:\n" + "\n".join(lines) + f"\n\nОбнови разбор отзывов {who}.")
