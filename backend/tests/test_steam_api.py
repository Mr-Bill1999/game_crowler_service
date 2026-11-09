"""Steam's player reviews through IUserReviewsService/GetAppReviews (the store's /appreviews was switched off in
October 2026), a redirect seen as the failure it is, and the pause after a run of failures. The answers here are
made up: no real review text is kept in the repository."""

import json

import httpx
import pytest

from app.sources import steam as steam_mod
from app.sources.steam import REVIEWS_API, SourcePaused, Steam
from app.sources.web import WebError


def review(i: int, hours_min: int = 600, voted_up: bool = True) -> dict:
    return {"recommendationid": str(i), "review": f"Review number {i} " + "word " * 30, "voted_up": voted_up,
            "language": "english", "timestamp_created": 1793000000 + i, "written_during_early_access": False,
            "author": {"playtime_at_review": hours_min, "playtime_forever": hours_min + 60}}


def client(handler) -> Steam:
    s = Steam(rps=1000)
    s.web.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


async def test_reviews_ask_the_web_api_for_recent_reviews_of_every_buyer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.url.params["input_json"])
        if body["cursor"] == "*":
            return httpx.Response(200, json={"response": {
                "query_summary": {"num_reviews": 2, "total_reviews": 3, "total_positive": 2,
                                  "review_score_desc": "Mostly Positive"},
                "reviews": [review(1), review(2, voted_up=False)], "cursor": "NEXT"}})
        return httpx.Response(200, json={"response": {"query_summary": {"num_reviews": 1},
                                                      "reviews": [review(3, 30)], "cursor": "NEXT"}})

    s = client(handler)
    out, totals = await s.reviews(4100, pages=5, known=set())
    await s.aclose()
    assert str(seen[0].url).startswith(REVIEWS_API)
    first = json.loads(seen[0].url.params["input_json"])
    assert first == {"appid": 4100, "filter": 1, "languages": ["all"], "review_type": 0, "purchase_type": 1,
                     "num_per_page": 100, "cursor": "*"}
    assert json.loads(seen[1].url.params["input_json"])["cursor"] == "NEXT"
    assert len(seen) == 2  # the cursor did not move on the second page: done
    assert [r.id for r in out] == ["1", "2", "3"] and out[2].hours == 0.5 and out[1].voted_up is False
    assert totals == {"total_reviews": 3, "total_positive": 2, "review_score_desc": "Mostly Positive"}


async def test_reviews_stop_at_a_known_one():
    def handler(request):
        return httpx.Response(200, json={"response": {"reviews": [review(7), review(8)], "cursor": "X"}})

    s = client(handler)
    out, _ = await s.reviews(1, pages=5, known={"8"})
    await s.aclose()
    assert [r.id for r in out] == ["7"]


async def test_a_redirect_is_an_error_not_a_page():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://store.steampowered.com/"})

    s = client(handler)
    with pytest.raises(WebError, match="redirected to https://store.steampowered.com/"):
        await s.reviews(1, pages=1, known=set())
    await s.aclose()


async def test_a_web_page_instead_of_json_says_so():
    def handler(request):
        return httpx.Response(200, text="<!DOCTYPE html><title>Welcome to Steam</title>",
                              headers={"content-type": "text/html; charset=UTF-8"})

    s = client(handler)
    with pytest.raises(WebError, match=r"not JSON \(text/html\)"):
        await s.reviews(1, pages=1, known=set())
    await s.aclose()


async def test_five_failures_in_a_row_pause_the_endpoint_and_report_once(monkeypatch):
    monkeypatch.setattr(steam_mod, "PAUSE_AFTER", 5)
    calls, paused = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://store.steampowered.com/"})

    async def on_pause(part, error):
        paused.append(part)

    s = client(handler)
    s.breaker.on_pause = on_pause
    for _ in range(5):
        with pytest.raises(WebError):
            await s.reviews(1, pages=1, known=set())
    with pytest.raises(SourcePaused):
        await s.reviews(1, pages=1, known=set())
    assert len(calls) == 5 and paused == ["steam_reviews"]
    # the news are another endpoint and keep working
    assert s.breaker.until.get("steam_news", 0) == 0
    await s.aclose()


async def test_success_clears_the_count():
    answers = iter([302, 302, 200, 302])

    def handler(request):
        code = next(answers)
        if code == 302:
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(200, json={"response": {"reviews": [], "query_summary": {}}})

    s = client(handler)
    for expect_error in (True, True, False, True):
        if expect_error:
            with pytest.raises(WebError):
                await s.reviews(1, pages=1, known=set())
        else:
            await s.reviews(1, pages=1, known=set())
    assert s.breaker.fails["steam_reviews"] == 1
    await s.aclose()
