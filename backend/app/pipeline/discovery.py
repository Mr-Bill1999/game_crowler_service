"""Finding games to ingest. Everything is a window of release dates, enumerated completely.

Quirks of the finder, measured on 15.10.2026:
- Offset pagination is unreliable when many games share a release date: the order of ties changes
  between requests, so neighbouring pages overlap and some games appear on no page at all
  (14.10: 84 games, 81 visible in one pass).
- Newest-first sorting ignores releaseDateMax: it lists everything from releaseDateMin up to "today" (its own
  today, which can lag behind UTC), so its total is not the window's. A one-day query for 13.10 answered 320 =
  84 + 104 + 132 for 13–15.10.
- Oldest-first respects both bounds and also lists future releases, so its total is the window's size.
So every window gets both passes, items outside the window are dropped, the oldest-first total is the target,
and a window still short of it is split in two.
"""

from dataclasses import dataclass
from datetime import UTC, date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.events.bus import EventBus
from app.models import Game, PipelineRun
from app.scraper.metacritic import Metacritic
from app.scraper.normalize import FinderItem


@dataclass
class WindowResult:
    items: list[FinderItem]
    total: int  # what the API says the window holds
    requests: int

    @property
    def complete(self) -> bool:
        return len(self.items) >= self.total


async def known_ids(session: AsyncSession, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = await session.execute(select(Game.id).where(Game.id.in_(ids)))
    return set(rows.scalars())


def fresh_items(items: list[FinderItem], known: set[int], seen: set[int]) -> list[FinderItem]:
    """Unknown and not already picked up in this pass (catalog and New Releases overlap)."""
    out = []
    for i in items:
        if i.id not in known and i.id not in seen:
            seen.add(i.id)
            out.append(i)
    return out


async def enumerate_window(mc: Metacritic, start: date, end: date, *, new_releases: bool) -> WindowResult:
    found: dict[int, FinderItem] = {}
    total, requests = 0, 0
    for ascending in (False, True):
        offset = 0
        while True:
            page = await mc.finder_page(new_releases=new_releases, offset=offset, date_min=start, date_max=end,
                                        ascending=ascending)
            requests += 1
            for i in page.items:
                if i.release_date and start <= i.release_date <= end:
                    found.setdefault(i.id, i)
            offset += len(page.items)
            dates = [i.release_date for i in page.items if i.release_date]
            past_window = dates and (dates[-1] < start if not ascending else dates[0] > end)
            if not page.items or offset >= page.total or past_window:
                break
        if ascending:
            total = page.total  # the only total that respects both bounds
    if len(found) >= total or end <= start:
        return WindowResult(list(found.values()), total, requests)

    mid = start + (end - start) // 2
    left = await enumerate_window(mc, start, mid, new_releases=new_releases)
    right = await enumerate_window(mc, mid + timedelta(days=1), end, new_releases=new_releases)
    for i in left.items + right.items:
        found.setdefault(i.id, i)
    return WindowResult(list(found.values()), left.total + right.total, requests + left.requests + right.requests)


async def _discover(mc: Metacritic, session: AsyncSession, bus: EventBus, start: date, end: date,
                    sources: list[tuple[str, bool]]) -> dict[str, list[FinderItem]]:
    found: dict[str, list[FinderItem]] = {}
    seen: set[int] = set()
    for source, new_releases in sources:
        w = await enumerate_window(mc, start, end, new_releases=new_releases)
        known = await known_ids(session, [i.id for i in w.items])
        found[source] = fresh_items(w.items, known, seen)
        await bus.emit("discovery.window", "ok" if w.complete else "error", source=source,
                       start=start, end=end, api_total=w.total, listed=len(w.items), new=len(found[source]),
                       requests=w.requests, missing=max(0, w.total - len(w.items)) or None)
    return found


async def discover_hourly(mc: Metacritic, session: AsyncSession, s: Settings, bus: EventBus,
                          today: date) -> dict[str, list[FinderItem]]:
    start = today - timedelta(days=s.discovery_window_days - 1)
    return await _discover(mc, session, bus, start, today, [("catalog", False), ("new_releases", True)])


async def discover_bootstrap(mc: Metacritic, session: AsyncSession, s: Settings, bus: EventBus,
                             today: date, days: int) -> list[FinderItem]:
    start = today - timedelta(days=days)
    return (await _discover(mc, session, bus, start, today, [("new_releases", True)]))["new_releases"]


def sweep_range(today: date, observed_from: date | None, s: Settings) -> tuple[date, date] | None:
    """Dates that left the hourly window and were seen by it at least once.

    The sweep exists for late additions: a game listed after its release date had already left the hourly
    window. Dates from before the service first ran were never watched, so anything there is backlog, not a late
    addition, and stays out (a clean start must not pull in two weeks of catalog). None means nothing to sweep."""
    if observed_from is None:
        return None
    start = max(today - timedelta(days=s.sweep_days), observed_from)
    end = today - timedelta(days=s.discovery_window_days)
    return (start, end) if start <= end else None


async def observed_from(session: AsyncSession, s: Settings) -> date | None:
    """First release date the hourly window has covered: the first finished hourly run's window start (UTC)."""
    first = await session.scalar(select(func.min(PipelineRun.started_at))
                                 .where(PipelineRun.kind == "hourly", PipelineRun.status == "done"))
    return first.astimezone(UTC).date() - timedelta(days=s.discovery_window_days - 1) if first else None


async def discover_sweep(mc: Metacritic, session: AsyncSession, s: Settings, bus: EventBus,
                         today: date) -> list[FinderItem]:
    """Games added to the catalog with an older release date never show up in the hourly window."""
    rng = sweep_range(today, await observed_from(session, s), s)
    if rng is None:
        await bus.emit("discovery.window", "skip", source="catalog", reason="нет дат, которые часовое окно уже видело")
        return []
    return (await _discover(mc, session, bus, *rng, [("catalog", False)]))["catalog"]
