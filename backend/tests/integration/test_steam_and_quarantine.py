"""What broke in production in October–November 2026, against a real Postgres:
- Steam's reviews failing took the store page, the news and the link down with them (one transaction);
- four Metacritic cards without a product failed every hour, forever."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.models import Document, Game, GameLink
from app.pipeline.quarantine import card_failed, cards_ok
from app.profile.pipeline import EnrichResult, collect_all
from app.scraper.normalize import EmptyCard
from app.sources.steam import StorePage
from app.sources.web import WebError

NOW = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
S = Settings(steam_review_pages=2)


class SteamStub:
    """Steam with working search, store and news, and reviews that redirect to the store front page."""

    async def resolve(self, title, developer):
        return 4100, {"name": title}

    async def store(self, appid):
        return StorePage(appid=appid, name="Dog Walk", developers=["Tiny Studio"], about="A dog walks " * 20,
                         meta="Жанры Steam: Casual", release="Nov 1, 2026", tags=["Dogs"])

    async def reviews(self, appid, pages, known):
        raise WebError("https://api.steampowered.com/...", 302, "redirected to https://store.steampowered.com/")

    async def news(self, appid):
        return [{"gid": "n1", "title": "Patch 1.1", "text": "Fixed the leash " * 20, "date": NOW}]


class NoCritics:
    async def full_text(self, url):
        return None


async def test_steam_reviews_failing_keeps_the_store_page_the_news_and_the_link(sessions):
    async with sessions() as s:
        s.add(Game(id=1, slug="dog-walk", title="Dog Walk", developer="Tiny Studio", tier="hot"))
        await s.commit()
    res = EnrichResult(slug="dog-walk")
    await collect_all(sessions, SteamStub(), NoCritics(), S, 1, res)

    async with sessions() as s:
        sources = sorted((await s.execute(select(Document.source).where(Document.game_id == 1))).scalars())
        link = await s.get(GameLink, (1, "steam"))
    assert "steam_store" in sources and "steam_news" in sources and "steam_reviews" not in sources
    assert link is not None and link.external_id == "4100"
    assert set(res.errors) == {"steam_reviews"} and "redirected" in res.errors["steam_reviews"]
    assert res.collected["steam_store"] and res.collected["steam_news"] == 1


async def test_a_card_without_product_is_paused_then_archived_and_a_success_clears_the_count(sessions):
    async with sessions() as s:
        s.add(Game(id=2, slug="gone", title="Gone", tier="dormant", next_refresh_at=NOW))
        s.add(Game(id=3, slug="flaky", title="Flaky", tier="hot", next_refresh_at=NOW))
        await s.commit()
    outcomes = []
    for i in range(3):
        async with sessions() as s:
            outcomes.append(await card_failed(s, "gone", EmptyCard("no product"), NOW + timedelta(hours=i)))
    assert [o.get("archived") for o in outcomes] == [None, None, True]

    for i in range(3):
        async with sessions() as s:
            out = await card_failed(s, "flaky", WebError("u", 503, "HTTP 503"), NOW + timedelta(hours=i))
    assert out == {"slug": "flaky", "fails": 3, "paused_h": 1}
    async with sessions() as s:
        flaky = await s.get(Game, 3)
        assert flaky.next_refresh_at == NOW + timedelta(hours=3) and flaky.archived_at is None
        await cards_ok(s, ["flaky"])
    async with sessions() as s:
        flaky, gone = await s.get(Game, 3), await s.get(Game, 2)
    assert flaky.fail_count == 0 and flaky.last_error is None
    assert gone.archived_at is not None and gone.next_refresh_at is None
    async with sessions() as s:
        assert await card_failed(s, "never-stored", KeyError("item"), NOW) is None
