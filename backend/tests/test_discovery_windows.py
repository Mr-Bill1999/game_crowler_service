"""enumerate_window against a fake finder that reproduces the overlapping-pages behaviour seen live."""

from datetime import date, timedelta

from app.pipeline.discovery import enumerate_window
from app.scraper.metacritic import FinderPage
from app.scraper.normalize import FinderItem

D14 = date(2026, 10, 14)



class FakeFinder:
    """Serves `catalog` in pages of `size`. With `overlap` > 0 each page after the first starts `overlap`
    items early, repeating them, and the last `overlap` items of the window are never served in that
    sort direction, like the live API does for release-date ties."""

    def __init__(self, catalog: list[FinderItem], size: int = 50, overlap: int = 0):
        self.catalog, self.size, self.overlap = catalog, size, overlap
        self.calls: list[tuple] = []

    async def finder_page(self, *, new_releases, offset=0, date_min=None, date_max=None, ascending=False):
        self.calls.append((date_min, date_max, ascending, offset))
        rows = [g for g in self.catalog if date_min <= g.release_date <= date_max]
        total = len(rows)
        rows = sorted(rows, key=lambda g: (g.release_date, g.id), reverse=not ascending)
        if offset and self.overlap:
            offset -= self.overlap  # pages overlap, the tail of the window is never reached
            if offset + self.size >= len(rows) - self.overlap:
                rows = rows[: len(rows) - self.overlap]
        return FinderPage(rows[offset: offset + self.size], total)


async def test_small_window_needs_one_request_per_direction():
    f = FakeFinder([FinderItem(i, f"g{i}", "", D14, None, 0, []) for i in range(30)])
    w = await enumerate_window(f, D14, D14, new_releases=False)
    assert w.complete and len(w.items) == 30 and w.requests == 2


async def test_overlapping_pages_are_filled_by_ascending_pass():
    f = FakeFinder([FinderItem(i, f"g{i}", "", D14, None, 0, []) for i in range(84)], overlap=3)
    w = await enumerate_window(f, D14, D14, new_releases=False)
    assert len(w.items) == 84 and w.complete
    assert any(asc for _, _, asc, _ in f.calls)


async def test_multi_day_window_is_split_when_both_passes_fall_short():
    days = [D14 - timedelta(days=k) for k in range(3)]
    catalog = [FinderItem(d.day * 1000 + i, f"g{d.day}-{i}", "", d, None, 0, []) for d in days for i in range(40)]

    class TiesAcrossDays(FakeFinder):
        async def finder_page(self, **kw):
            page = await super().finder_page(**kw)
            if kw["date_min"] != kw["date_max"]:  # a multi-day window always loses two games
                return FinderPage(page.items[2:] if kw["offset"] == 0 else page.items, page.total)
            return page

    f = TiesAcrossDays(catalog)
    w = await enumerate_window(f, days[-1], D14, new_releases=False)
    assert w.complete and len(w.items) == 120


class ReleasedOnlyWhenNewestFirst(FakeFinder):
    """Live behaviour: newest-first hides games released today or later, oldest-first shows them."""

    def __init__(self, catalog, today: date, **kw):
        super().__init__(catalog, **kw)
        self.today = today

    async def finder_page(self, **kw):
        if kw.get("ascending"):
            return await super().finder_page(**kw)
        visible = [g for g in self.catalog if g.release_date < self.today]
        saved, self.catalog = self.catalog, visible
        try:
            return await super().finder_page(**kw)
        finally:
            self.catalog = saved


async def test_todays_releases_come_from_the_ascending_pass():
    today = D14 + timedelta(days=1)
    catalog = [FinderItem(i, f"y{i}", "", D14, None, 0, []) for i in range(20)]
    catalog += [FinderItem(100 + i, f"t{i}", "", today, None, 0, []) for i in range(15)]
    f = ReleasedOnlyWhenNewestFirst(catalog, today)
    w = await enumerate_window(f, D14, today, new_releases=False)
    assert w.complete and w.total == 35 and {i.release_date for i in w.items} == {D14, today}


async def test_items_outside_the_window_are_dropped():
    class Leaky(FakeFinder):
        async def finder_page(self, **kw):
            page = await super().finder_page(**kw)
            stray = FinderItem(999, "stray", "", D14 + timedelta(days=5), None, 0, [])
            return FinderPage([*page.items, stray], page.total)

    f = Leaky([FinderItem(i, f"g{i}", "", D14, None, 0, []) for i in range(10)])
    w = await enumerate_window(f, D14, D14, new_releases=False)
    assert 999 not in {i.id for i in w.items} and len(w.items) == 10


class NewestFirstIgnoresMax(FakeFinder):
    """Live behaviour since the evening of 15.10: newest-first returns everything from date_min to today."""

    def __init__(self, catalog, today: date, **kw):
        super().__init__(catalog, **kw)
        self.today = today

    async def finder_page(self, **kw):
        if not kw.get("ascending"):
            kw = {**kw, "date_max": self.today}
        return await super().finder_page(**kw)


async def test_inflated_newest_first_total_does_not_fail_the_window():
    today = D14 + timedelta(days=1)
    catalog = [FinderItem(d.day * 1000 + i, f"g{d.day}-{i}", "", d, None, 0, [])
               for d in (D14 - timedelta(days=1), D14, today) for i in range(30)]
    f = NewestFirstIgnoresMax(catalog, today)
    w = await enumerate_window(f, D14 - timedelta(days=1), D14 - timedelta(days=1), new_releases=False)
    assert w.complete and w.total == 30 and len(w.items) == 30
    assert w.requests <= 4  # no splitting, desc pass stops once dates leave the window


def test_sweep_skips_dates_the_hourly_window_never_watched():
    from app.core.config import Settings
    from app.pipeline.discovery import sweep_range

    s = Settings(sweep_days=14, discovery_window_days=3)
    # clean start on 15.10: the first hourly run watched 13-15.10, so on 16.10 only 13.10 has left the window
    assert sweep_range(date(2026, 10, 16), date(2026, 10, 13), s) == (date(2026, 10, 13), date(2026, 10, 13))
    # the same night of the start: everything watched is still in the hourly window
    assert sweep_range(date(2026, 10, 15), date(2026, 10, 13), s) is None
    # once the service has run for weeks, the full 14-day depth applies
    assert sweep_range(date(2026, 11, 20), date(2026, 10, 13), s) == (date(2026, 11, 6), date(2026, 11, 17))
    # never ran an hourly cycle: nothing was watched
    assert sweep_range(date(2026, 10, 16), None, s) is None
