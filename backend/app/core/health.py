"""Health of every outside source and of the service itself: is it working right now, and since when not.

Three signals per source:
- every outside call records its outcome (success or a class of error) in an hourly counter in Redis;
- once an hour a canary asks a known, stable thing (CS2 on Steam, a well-known game on Metacritic) and checks the
  shape of the answer, so a changed or switched-off API shows within the hour even when no game needed it;
- freshness: how long since new data of each kind arrived, for failures that raise no error at all.

`judge` turns them into one state per source: ok, degraded, stale, quota, down or unknown. The worker evaluates
the states every hour (app.pipeline.tasks.check_health), emits an event when one changes, and DevPanel shows them.
Recording never raises: an outage of Redis must not become an outage of the pipeline.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis

log = logging.getLogger(__name__)

# Order is the order DevPanel shows them in.
SOURCES: dict[str, str] = {
    "metacritic": "Metacritic",
    "steam_store": "Steam: магазин",
    "steam_reviews": "Steam: отзывы",
    "steam_news": "Steam: новости",
    "critics": "Сайты критиков",
    "gemini": "Gemini",
    "embeddings": "Эмбеддинги",
    "youtube": "YouTube",
    "worker": "Воркер",
    "backup": "Бэкапы",
    "server": "Сервер",
}
KINDS = ("ok", "redirect", "not_json", "http_4xx", "http_429", "http_5xx", "network", "parse", "quota", "paused",
         "other")
KIND_LABEL = {"redirect": "переадресация", "not_json": "ответ не JSON", "http_4xx": "ошибка 4xx",
              "http_429": "лимит запросов (429)", "http_5xx": "ошибка сервера 5xx", "network": "сеть или таймаут",
              "parse": "неожиданная форма ответа", "quota": "дневная квота", "paused": "источник на паузе",
              "other": "другая ошибка"}
STATES = ("down", "quota", "degraded", "stale", "ok", "unknown")  # worst first
COUNTER_TTL_S = 15 * 86400
WINDOW_H = 2  # counters judged over the current and the previous hour


@dataclass(frozen=True)
class Rule:
    canary: bool = False  # an hourly canary exists for this source
    min_attempts: int = 5  # "down" needs at least this many calls with no success
    fail_rate: float = 0.5  # "degraded" from this share of failed calls (with 10+ calls)
    window_h: int = WINDOW_H
    stale_h: float | None = None  # "stale" when nothing new arrived for longer than this


RULES: dict[str, Rule] = {
    "metacritic": Rule(canary=True, stale_h=3),
    "steam_store": Rule(canary=True, stale_h=72),
    "steam_reviews": Rule(canary=True, stale_h=48),
    "steam_news": Rule(canary=True),
    # hundreds of sites, many of them paywalled or gone: judged over a day, and only a near-total failure counts
    "critics": Rule(window_h=24, min_attempts=20, fail_rate=0.7, stale_h=96),
    "gemini": Rule(min_attempts=3, stale_h=48),
    "embeddings": Rule(min_attempts=3),
    "youtube": Rule(min_attempts=3),
}

_redis: Redis | None = None


def attach(redis: Redis | None) -> None:
    """The worker hands its Redis client here at startup; without one, recording is a no-op (tests, CLI)."""
    global _redis
    _redis = redis


def hour_key(source: str, t: datetime) -> str:
    return f"health:c:{source}:{t.astimezone(UTC):%Y%m%d%H}"


def classify(e: BaseException) -> str:
    """The class of an outside failure, from the exception the clients raise."""
    name = type(e).__name__
    text = str(e).lower()
    status = next((v for v in (getattr(e, "status", None), getattr(e, "code", None)) if isinstance(v, int)), None)
    if name == "QuotaExhausted" or "daily limit" in text or "quotaexceeded" in text or "over their daily" in text:
        return "quota"
    if name == "SourcePaused":
        return "paused"
    if "redirect" in text:
        return "redirect"
    if "not json" in text or "не json" in text:
        return "not_json"
    if isinstance(status, int):
        if 300 <= status < 400:
            return "redirect"
        if status == 429:
            return "http_429"
        if 400 <= status < 500:
            return "http_4xx"
        if status >= 500:
            return "http_5xx"
    if isinstance(e, (httpx.HTTPError, TimeoutError, ConnectionError)) or "timeout" in text or "недоступен" in text:
        return "network"
    if isinstance(e, (KeyError, ValueError, TypeError, IndexError)):
        return "parse"
    return "other"


async def record(source: str, kind: str = "ok", detail: str | None = None, now: datetime | None = None) -> None:
    redis = _redis
    if redis is None:
        return
    now = now or datetime.now(UTC)
    try:
        async with redis.pipeline(transaction=False) as p:
            key = hour_key(source, now)
            p.hincrby(key, kind, 1)
            p.expire(key, COUNTER_TTL_S)
            if kind == "ok":
                p.hset(f"health:last:{source}", "ok_at", now.isoformat())
            else:
                p.hset(f"health:last:{source}", mapping={"err_at": now.isoformat(), "err_kind": kind,
                                                         "err_text": (detail or "")[:300]})
            await p.execute()
    except Exception:  # noqa: BLE001 - health must never break the work it watches
        log.warning("health record failed: %s %s", source, kind)


@asynccontextmanager
async def track(source: str) -> AsyncIterator[None]:
    """Record the outcome of one outside call: `async with health.track("steam_reviews"): ...`"""
    try:
        yield
    except Exception as e:
        await record(source, classify(e), f"{type(e).__name__}: {e}")
        raise
    else:
        await record(source, "ok")


async def counts(redis: Redis, source: str, now: datetime, hours: int) -> list[dict[str, int]]:
    """Per-hour counters for the last `hours` hours, oldest first."""
    keys = [hour_key(source, now - timedelta(hours=h)) for h in range(hours - 1, -1, -1)]
    async with redis.pipeline(transaction=False) as p:
        for k in keys:
            p.hgetall(k)
        rows = await p.execute()
    return [{k: int(v) for k, v in (r or {}).items()} for r in rows]


def total(rows: list[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        for k, v in r.items():
            out[k] = out.get(k, 0) + v
    return out


@dataclass
class Verdict:
    state: str
    reason: str


def judge(rule: Rule, window: dict[str, int], canary: dict | None, age_h: float | None) -> Verdict:
    """One source's state from its counters over the rule's window, its latest canary and its freshness."""
    ok = window.get("ok", 0)
    quota = window.get("quota", 0)
    fails = {k: v for k, v in window.items() if k not in ("ok", "quota", "paused") and v}
    failed = sum(fails.values())
    attempts = ok + failed
    top = max(fails, key=fails.get) if fails else None
    if rule.canary and canary is not None and not canary.get("ok"):
        return Verdict("down", f"контрольный запрос не прошёл: {canary.get('detail') or 'без ответа'}")
    if attempts >= rule.min_attempts and ok == 0:
        return Verdict("down", f"успешно 0 из {attempts} за {rule.window_h} ч, чаще всего: {KIND_LABEL[top]}")
    if quota and ok == 0:
        return Verdict("quota", "дневная квота исчерпана, продолжит после сброса")
    if attempts >= 10 and failed / attempts >= rule.fail_rate:
        return Verdict("degraded", f"{round(100 * failed / attempts)} % ошибок за {rule.window_h} ч, "
                                   f"чаще всего: {KIND_LABEL[top]}")
    if window.get("paused"):
        return Verdict("degraded", "источник на паузе после серии отказов")
    if rule.stale_h is not None and age_h is not None and age_h > rule.stale_h:
        return Verdict("stale", f"новых данных нет {fmt_age(age_h)} (норма — до {fmt_age(rule.stale_h)})")
    if attempts or (canary and canary.get("ok")) or (age_h is not None):
        return Verdict("ok", f"успешно {ok} из {attempts} за {rule.window_h} ч" if attempts else "работает")
    return Verdict("unknown", f"обращений не было {rule.window_h} ч")


def fmt_age(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))} мин"
    if h < 48:
        return f"{round(h)} ч"
    return f"{round(h / 24)} дн"


def worst(states: list[str]) -> str:
    return min(states, key=STATES.index) if states else "unknown"


async def save_canary(redis: Redis, source: str, ok: bool, detail: str, ms: int, now: datetime) -> None:
    await redis.hset(f"health:canary:{source}", mapping={"at": now.isoformat(), "ok": int(ok), "detail": detail[:300],
                                                         "ms": ms})


async def read_canary(redis: Redis, source: str) -> dict | None:
    c = await redis.hgetall(f"health:canary:{source}")
    if not c:
        return None
    return {"at": c.get("at"), "ok": c.get("ok") == "1", "detail": c.get("detail") or "", "ms": int(c.get("ms") or 0)}


async def canary(redis: Redis, source: str, probe, now: datetime) -> dict:
    """Run one canary: `probe` is an async callable returning a short description of what it saw, or raising."""
    t0 = time.monotonic()
    try:
        detail = await probe()
        ok = True
    except Exception as e:  # noqa: BLE001
        detail, ok = f"{KIND_LABEL.get(classify(e), 'ошибка')}: {e}"[:300], False
    ms = round(1000 * (time.monotonic() - t0))
    await save_canary(redis, source, ok, detail, ms, now)
    return {"at": now.isoformat(), "ok": ok, "detail": detail, "ms": ms}


async def save_states(redis: Redis, states: dict[str, dict], now: datetime) -> dict[str, dict]:
    """Store the new states; returns the ones that changed, with their previous state."""
    old = {k: json.loads(v) for k, v in (await redis.hgetall("health:state")).items()}
    changed = {}
    for source, st in states.items():
        prev = old.get(source)
        if prev is None or prev.get("state") != st["state"]:
            st["since"] = now.isoformat()
            changed[source] = {**st, "was": prev.get("state") if prev else None}
        else:
            st["since"] = prev.get("since") or now.isoformat()
    async with redis.pipeline(transaction=False) as p:
        p.hset("health:state", mapping={k: json.dumps(v, ensure_ascii=False) for k, v in states.items()})
        p.set("health:evaluated_at", now.isoformat())
        await p.execute()
    return changed


EVENT_STATUS = {"ok": "ok", "degraded": "retry", "stale": "retry", "quota": "skip", "down": "error",
                "unknown": "skip"}
