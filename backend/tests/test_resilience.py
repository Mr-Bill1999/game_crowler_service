"""External services failing the way they really fail, through the same fault layer the test stand uses."""

import httpx
import pytest

from app.ai import gemini as gemini_mod
from app.ai.base import LLMResult
from app.core import faults
from app.core.config import Settings, get_settings
from app.scraper import client as client_mod
from app.scraper.client import FetchError, MCClient
from app.sources.youtube import YouTube, YouTubeError


class Bus:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    async def emit(self, stage, status="ok", **fields):
        self.events.append((stage, status, fields))


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []

    async def fake_sleep(s):
        waited.append(s)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gemini_mod.asyncio, "sleep", fake_sleep)
    return waited


@pytest.fixture
def inject(monkeypatch):
    def set_faults(spec: str):
        monkeypatch.setattr(get_settings(), "faults", spec)
        monkeypatch.setattr(get_settings(), "tape_mode", "live")
        faults.reset()

    yield set_faults
    faults.reset()


def metacritic(handler) -> tuple[MCClient, Bus]:
    bus = Bus()
    s = Settings(max_attempts=4, rate_limit_rps=1000)
    http = httpx.AsyncClient(base_url="https://mc.test", transport=httpx.MockTransport(handler))
    return MCClient(s, bus, http=http), bus


def ok(request):
    return httpx.Response(200, json={"data": {"items": [], "totalResults": 0}})


async def test_transient_5xx_and_timeouts_are_retried(inject, sleeps):
    inject("metacritic:http503:1:2; metacritic:timeout:1:1")
    mc, bus = metacritic(ok)
    assert await mc.get_json("discovery.catalog", "/finder") == {"data": {"items": [], "totalResults": 0}}
    assert [s for _, s, _ in bus.events] == ["retry", "retry", "retry", "ok"]
    assert len(sleeps) == 3


async def test_cloudflare_page_with_200_is_not_taken_for_data(inject, sleeps):
    inject("metacritic:html")
    mc, bus = metacritic(ok)
    with pytest.raises(FetchError, match="not JSON"):
        await mc.get_json("fetch.card", "/composer/x")
    assert bus.events[-1][1] == "error"


async def test_client_errors_fail_at_once(inject, sleeps):
    inject("metacritic:http404")
    mc, _ = metacritic(ok)
    with pytest.raises(FetchError) as e:
        await mc.get_json("fetch.card", "/composer/gone")
    assert e.value.status == 404 and sleeps == []


async def test_retry_after_is_honoured(inject, sleeps):
    inject("metacritic:http429:1:1")
    mc, _ = metacritic(ok)
    await mc.get_json("discovery.top", "/finder")
    assert sleeps[0] >= 2  # the fault answers Retry-After: 2


class FakeGemini(gemini_mod.GeminiProvider):
    def __init__(self):  # no SDK client: the single attempt is faked below
        self.max_attempts, self.default_model = 4, "m"
        self.calls = 0

    async def _generate_once(self, **kw):
        self.calls += 1
        return LLMResult(data={"ok": True}, model="m", tokens_in=1, tokens_out=1, cost_usd=0)


async def test_gemini_per_minute_limit_is_waited_out(inject, sleeps):
    inject("gemini:http429:1:2")
    g = FakeGemini()
    r = await g.generate_json(system="s", prompt="p", schema={})
    assert r.data == {"ok": True} and g.calls == 1 and len(sleeps) == 2 and max(sleeps) < 60


async def test_gemini_daily_limit_fails_fast_instead_of_sleeping_for_hours(inject, sleeps):
    inject("gemini:daily")
    g = FakeGemini()
    with pytest.raises(gemini_mod.errors.ClientError):
        await g.generate_json(system="s", prompt="p", schema={})
    assert sleeps == [] and g.calls == 0


async def test_youtube_quota_and_broken_bodies_become_youtube_errors(inject):
    yt = YouTube("k", http=httpx.AsyncClient(base_url="https://yt.test", transport=httpx.MockTransport(ok)))
    inject("youtube:quota")
    with pytest.raises(YouTubeError, match="quotaExceeded"):
        await yt.search("Any Game")
    inject("youtube:html")
    with pytest.raises(YouTubeError, match="не JSON"):
        await yt.search("Any Game")
    inject("youtube:timeout")
    with pytest.raises(YouTubeError, match="недоступен"):
        await yt.search("Any Game")


def test_fault_spec_parsing():
    f = faults.parse("metacritic:http503:0.3; gemini:daily:1:2 ;youtube:quota")
    assert [(x.source, x.kind, x.rate, x.left) for x in f] == [
        ("metacritic", "http503", 0.3, None), ("gemini", "daily", 1.0, 2), ("youtube", "quota", 1.0, None)]
    with pytest.raises(faults.FaultSpecError):
        faults.parse("metacritic")
