"""The hourly health check: canaries, freshness from the database, the server's own numbers, then one state per
source (rules in app/core/health.py). Runs in the worker at :45, between the hourly cycles."""

import shutil
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core import health
from app.core.health import RULES, SOURCES, Verdict, fmt_age, judge
from app.models import Analysis, Document, Game, GameProfile, PipelineRun

CANARY_SLUG = "elden-ring"  # a card Metacritic will keep for years
BACKUP_MAX_H = 26
RESTORE_MAX_DAYS = 8
MEMORY_WARN = 0.85  # share of the host's memory in use
DISK_WARN = 0.80


def _age_h(at: datetime | None, now: datetime) -> float | None:
    return None if at is None else (now - at).total_seconds() / 3600


async def freshness(session, now: datetime) -> dict[str, float | None]:
    """Hours since new data of each kind. Steam and critics count only while profiles are being built: with no
    profile work an old newest document says nothing."""
    newest = dict((await session.execute(
        select(Document.source, func.max(Document.fetched_at)).group_by(Document.source))).all())
    profiles_at = await session.scalar(select(func.max(GameProfile.enriched_at)))
    profiling = profiles_at is not None and (now - profiles_at).total_seconds() < 6 * 3600
    ages = {
        "metacritic": _age_h(await session.scalar(select(func.max(Game.checked_at))), now),
        "gemini": _age_h(await session.scalar(select(func.max(Analysis.created_at))), now),
    }
    for source, doc in (("steam_store", "steam_store"), ("steam_reviews", "steam_reviews"),
                        ("critics", "critic_full")):
        ages[source] = _age_h(newest.get(doc), now) if profiling else None
    return ages


def memory_used() -> tuple[float | None, int | None]:
    """Share of the host's memory in use (/proc/meminfo is the host's inside a container) and this container's
    peak, in bytes, when cgroup v2 reports it."""
    share = peak = None
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        share = 1 - info["MemAvailable"] / info["MemTotal"]
    except (OSError, KeyError, ValueError):
        pass
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.max_usage_in_bytes"):
        try:
            with open(path) as f:
                peak = int(f.read().strip())
                break
        except (OSError, ValueError):
            continue
    return share, peak


async def server_verdicts(redis: Redis, session, now: datetime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # worker: the hourly cycle must have started within the last 70 minutes
    last_run = await session.scalar(select(func.max(PipelineRun.started_at)).where(PipelineRun.kind == "hourly"))
    age = _age_h(last_run, now)
    out["worker"] = (Verdict("ok", f"часовой прогон {fmt_age(age)} назад") if age is not None and age < 70 / 60
                     else Verdict("down", "часовой прогон не начинался " + (fmt_age(age) if age else "ни разу")))
    # backups: written to Redis by deploy/backup.sh and deploy/restore-check.sh on the host
    b = await redis.hgetall("health:backup")
    r = await redis.hgetall("health:restore")
    b_age = _age_h(datetime.fromisoformat(b["at"]), now) if b.get("at") else None
    r_age = _age_h(datetime.fromisoformat(r["at"]), now) if r.get("at") else None
    if b_age is None:
        out["backup"] = Verdict("unknown", "бэкап по расписанию ещё не делался")
    elif b_age > BACKUP_MAX_H:
        out["backup"] = Verdict("down", f"последний бэкап {fmt_age(b_age)} назад")
    elif r.get("ok") == "0":
        out["backup"] = Verdict("degraded", f"проверка восстановления не прошла: {r.get('detail', '')[:120]}")
    elif r_age is not None and r_age > RESTORE_MAX_DAYS * 24:
        out["backup"] = Verdict("stale", f"восстановление не проверялось {fmt_age(r_age)}")
    else:
        size = int(b.get("bytes") or 0) / 2**20
        checked = f", восстановление проверено {fmt_age(r_age)} назад" if r_age is not None else ""
        out["backup"] = Verdict("ok", f"{fmt_age(b_age)} назад, {size:.0f} МБ{checked}")
    # server: memory and disk
    share, peak = memory_used()
    disk = shutil.disk_usage("/")
    disk_share = disk.used / disk.total
    parts = []
    if share is not None:
        parts.append(f"память {round(100 * share)} %")
    if peak:
        parts.append(f"пик воркера {peak / 2**30:.1f} ГБ")
    parts.append(f"диск {round(100 * disk_share)} %")
    bad = (share is not None and share > MEMORY_WARN) or disk_share > DISK_WARN
    out["server"] = Verdict("degraded" if bad else "ok", ", ".join(parts))
    return {k: {"state": v.state, "reason": v.reason} for k, v in out.items()}


async def run_canaries(redis: Redis, mc, steam, now: datetime) -> dict[str, dict]:
    async def metacritic() -> str:
        data = await mc.composer(CANARY_SLUG)
        names = {c["meta"]["componentName"] for c in data.get("components") or []}
        if "product" not in names:
            raise ValueError("composer: no product component")
        return "карточка открывается, форма ответа прежняя"

    probes = {"metacritic": metacritic, "steam_reviews": steam.canary_reviews, "steam_store": steam.canary_store,
              "steam_news": steam.canary_news}
    return {source: await health.canary(redis, source, probe, now) for source, probe in probes.items()}


async def evaluate(redis: Redis, session, now: datetime, canaries: dict[str, dict]) -> dict[str, dict]:
    ages = await freshness(session, now)
    states: dict[str, dict] = {}
    for source, rule in RULES.items():
        rows = await health.counts(redis, source, now, rule.window_h)
        canary = canaries.get(source) or (await health.read_canary(redis, source) if rule.canary else None)
        v = judge(rule, health.total(rows), canary, ages.get(source))
        states[source] = {"state": v.state, "reason": v.reason}
    states.update(await server_verdicts(redis, session, now))
    return {k: states[k] for k in SOURCES if k in states}

