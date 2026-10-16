"""Live checks against Metacritic. Skipped unless GA_LIVE=1.

  GA_LIVE=1 pytest tests/timing -s            contract + timing for 20 cards
  GA_LIVE=1 GA_TIMING_CARDS=50 pytest ...     bigger sample
"""

import os
import time

import pytest

from app.core.config import Settings
from app.scraper.client import MCClient
from app.scraper.metacritic import Metacritic
from app.scraper.normalize import parse_composer

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("GA_LIVE") != "1", reason="set GA_LIVE=1")]


class NullBus:
    def __init__(self):
        self.events: list[tuple] = []

    async def emit(self, stage, status="ok", **fields):
        self.events.append((stage, status, fields))


async def make() -> tuple[Metacritic, MCClient, NullBus]:
    s = Settings()
    bus = NullBus()
    client = MCClient(s, bus)
    return Metacritic(client, s), client, bus


async def test_contract_composer_components():
    mc, client, _ = await make()
    try:
        card = parse_composer(await mc.composer("elden-ring"), Settings().mc_image_base)
    finally:
        await client.aclose()
    assert card.title == "Elden Ring" and card.platforms and card.metascore


async def test_timing_n_cards():
    n = int(os.getenv("GA_TIMING_CARDS", "20"))
    mc, client, bus = await make()
    s = Settings()
    t0 = time.perf_counter()
    try:
        page = await mc.finder_page(new_releases=True, page=0)
        reviews = 0
        for item in page.items[:n]:
            card = parse_composer(await mc.composer(item.slug), s.mc_image_base)
            for p in card.platforms:
                if p.critic_count:
                    reviews += len(await mc.reviews("critic", card.slug, p.slug, None))
                if card.user_count:
                    reviews += len(await mc.reviews("user", card.slug, p.slug, None))
    finally:
        await client.aclose()
    sec = time.perf_counter() - t0
    oks = [f for st, status, f in bus.events if status == "ok"]
    errors = [e for e in bus.events if e[1] == "error"]
    print(f"\n{n} cards: {sec:.1f}s, {len(oks)} requests, {reviews} reviews, "
          f"{sum(f.get('bytes', 0) for f in oks) / 1e6:.1f} MB, {len(errors)} errors, "
          f"{s.rate_limit_rps} rps limit")
    assert not errors
