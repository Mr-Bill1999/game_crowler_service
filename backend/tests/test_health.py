"""Source health: how failures are classified, and how counters, canary and freshness become one state."""

import httpx
import pytest

from app.ai.gemini import QuotaExhausted
from app.core.health import RULES, Rule, classify, judge, total, worst
from app.pipeline.quarantine import pause_for
from app.scraper.client import FetchError
from app.scraper.normalize import EmptyCard, parse_composer
from app.sources.steam import SourcePaused
from app.sources.web import WebError


@pytest.mark.parametrize(("error", "kind"), [
    (WebError("u", 302, "redirected to https://store.steampowered.com/"), "redirect"),
    (WebError("u", 200, "not JSON (text/html)"), "not_json"),
    (WebError("u", 429, "HTTP 429"), "http_429"),
    (WebError("u", 403, "not OK"), "http_4xx"),
    (FetchError("/x", 503, "attempts exhausted: http 503"), "http_5xx"),
    (WebError("u", None, "ConnectTimeout"), "network"),
    (httpx.ReadTimeout("slow"), "network"),
    (QuotaExhausted("all 2 Gemini keys are over their daily limit"), "quota"),
    (SourcePaused("steam_reviews", 0), "paused"),
    (EmptyCard("Metacritic card has no product item"), "parse"),
    (KeyError("item"), "parse"),
    (RuntimeError("?"), "other"),
])
def test_classify(error, kind):
    assert classify(error) == kind


CANARY = Rule(canary=True, stale_h=48)


def test_failed_canary_is_down_even_without_traffic():
    v = judge(CANARY, {}, {"ok": False, "detail": "переадресация: redirected to /"}, None)
    assert v.state == "down" and "контрольный запрос" in v.reason


def test_no_success_in_enough_calls_is_down_and_names_the_commonest_error():
    v = judge(CANARY, {"redirect": 40, "network": 2}, {"ok": True}, 1)
    assert v.state == "down" and "0 успешных из 42" in v.reason and "переадресация" in v.reason


def test_few_failures_are_not_an_outage():
    assert judge(CANARY, {"ok": 30, "network": 3}, {"ok": True}, 1).state == "ok"
    assert judge(CANARY, {"network": 2}, {"ok": True}, 1).state == "ok"  # below min_attempts


def test_half_failing_is_degraded():
    v = judge(CANARY, {"ok": 6, "http_429": 9}, {"ok": True}, 1)
    assert v.state == "degraded" and "60 %" in v.reason


def test_quota_is_its_own_state():
    assert judge(RULES["gemini"], {"quota": 12}, None, 2).state == "quota"


def test_silence_beyond_the_window_is_stale():
    v = judge(CANARY, {"ok": 5}, {"ok": True}, 60)
    assert v.state == "stale" and "2 дн" in v.reason


def test_nothing_known_is_unknown():
    assert judge(RULES["youtube"], {}, None, None).state == "unknown"


def test_critics_tolerate_many_dead_sites():
    assert judge(RULES["critics"], {"ok": 8, "http_4xx": 12}, None, 10).state == "ok"
    assert judge(RULES["critics"], {"ok": 2, "http_4xx": 30}, None, 10).state == "degraded"


def test_total_and_worst():
    assert total([{"ok": 1}, {"ok": 2, "network": 1}]) == {"ok": 3, "network": 1}
    assert worst(["ok", "stale", "down", "quota"]) == "down"
    assert worst([]) == "unknown"


def test_quarantine_pauses_grow_and_stop_at_a_week():
    assert [pause_for(n) for n in (1, 2)] == [None, None]
    assert [pause_for(n).total_seconds() / 3600 for n in (3, 4, 5, 6, 20)] == [1, 2, 4, 8, 168]


def test_card_without_product_is_an_empty_card():
    data = {"components": [{"meta": {"componentName": "product"}, "data": {"item": None}}]}
    with pytest.raises(EmptyCard):
        parse_composer(data, "https://img/")
    with pytest.raises(EmptyCard):
        parse_composer({}, "https://img/")
