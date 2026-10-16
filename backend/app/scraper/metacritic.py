"""Metacritic backend endpoints. Reviews are fetched per platform: without one the API returns only the lead platform."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.config import Settings
from app.scraper.client import MCClient
from app.scraper.normalize import FinderItem, critic_review_id, parse_finder_item, user_review_id

CRITIC_PAGE = 10  # the critic endpoint ignores `limit`


@dataclass
class FinderPage:
    items: list[FinderItem]
    total: int


class Metacritic:
    def __init__(self, client: MCClient, settings: Settings):
        self.c = client
        self.s = settings

    async def finder_page(self, *, new_releases: bool, offset: int = 0, date_min: date | None = None,
                          date_max: date | None = None, ascending: bool = False) -> FinderPage:
        params: dict[str, str | int] = {
            "sortBy": "releaseDate" if ascending else "-releaseDate",
            "productType": "games",
            "offset": offset,
            "limit": self.s.finder_page_size,
        }
        if new_releases:
            params["metaScoreMin"] = 1
        if date_min:
            params["releaseDateMin"] = date_min.isoformat()
        if date_max:
            params["releaseDateMax"] = date_max.isoformat()
        stage = "discovery.new_releases" if new_releases else "discovery.catalog"
        d = (await self.c.get_json(stage, "/finder/metacritic/web", params))["data"]
        return FinderPage([parse_finder_item(i) for i in d["items"]], d["totalResults"])

    async def new_releases_top(self, limit: int = 20) -> list[FinderItem]:
        """The "New Releases" block on metacritic.com/game/: the same query the site sends, verified against the page.
        Newest released games that already have a Metascore."""
        params = {"sortBy": "-releaseDate", "metaScoreMin": 1, "offset": 0, "limit": limit, "mcoTypeId": 13}
        d = (await self.c.get_json("discovery.top", "/finder/metacritic/web", params))["data"]
        return [parse_finder_item(i) for i in d["items"]]

    async def composer(self, slug: str) -> dict:
        return await self.c.get_json("fetch.card", f"/composer/metacritic/pages/games/{slug}/web", slug=slug)

    async def reviews(self, kind: str, slug: str, platform: str, known: set[str] | None) -> list[dict]:
        """All reviews when `known` is None; otherwise newest-first until the first already stored one."""
        path = f"/reviews/metacritic/{kind}/games/{slug}/platform/{platform}/web"
        page_size = CRITIC_PAGE if kind == "critic" else self.s.user_reviews_page_size
        key: Callable[[dict], str] = critic_review_id if kind == "critic" else user_review_id
        stage = f"fetch.{kind}"

        async def page(offset: int) -> tuple[list[dict], int]:
            d = (await self.c.get_json(stage, path, {"offset": offset, "limit": page_size, "sort": "date"},
                                       slug=slug))["data"]
            return d["items"], d["totalResults"]

        items, total = await page(0)
        if known is None:
            # Full fetch: once the total is known, the remaining pages go in parallel.
            rest = await asyncio.gather(*(page(o) for o in range(len(items), total, page_size)))
            return items + [i for batch, _ in rest for i in batch]

        out = list(items)
        offset = len(items)
        while not any(key(i) in known for i in items) and items and offset < total:
            items, total = await page(offset)
            out += items
            offset += len(items)
        return out
