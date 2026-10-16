from datetime import date

from app.ai import analyze as an
from app.ai.base import LLMResult
from app.models import Game, Review


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def generate_json(self, *, system, prompt, schema, model=None, thinking_level=None):
        self.calls.append((system, prompt))
        if "summary" in schema["properties"]:
            data = {"summary": f"s{len(self.calls)}", "consensus": "positive", "liked": ["a"], "disliked": ["b"],
                    "notable_quotes": []}
        else:
            data = {"genre": "RPG", "core_loop": "x", "mechanics": [], "setting": "y", "unique_features": [],
                    "comparable_to": []}
        return LLMResult(data, model or "m", 100, 10, 0.001, 0.5)


def game() -> Game:
    return Game(id=1, slug="g", title="Game", genres=["RPG"], critic_count=5, user_count=40, platforms=[],
                release_date=date(2026, 10, 1))


def reviews(kind: str, n: int, size: int = 50) -> list[Review]:
    return [Review(id=f"{kind}{i}", game_id=1, kind=kind, score=8, quote="x" * size, platform="PC",
                   date=date(2026, 10, 2)) for i in range(n)]


async def test_small_sets_use_one_call_per_task():
    p = FakeProvider()
    res = await an.analyze_game(p, game(), reviews("critic", 5), reviews("user", 20), "m")
    assert all(res[k].data for k in ("critic", "user", "essence"))
    assert len(p.calls) == 3
    assert res["critic"].cost_usd == 0.001 and res["user"].reviews_seen == 20


async def test_large_sets_go_through_map_reduce(monkeypatch):
    monkeypatch.setattr(an, "chunk_reviews", lambda rs: [["l"] * 10, ["l"] * 10, ["l"] * 5])
    p = FakeProvider()
    res = await an.analyze_game(p, game(), reviews("critic", 2), reviews("user", 25), "m")
    assert res["critic"].data is None and not res["critic"].calls  # below the minimum of 3
    assert len(res["user"].calls) == 4  # three parts plus the reduce step
    assert any("часть 3 из 3" in prompt for _, prompt in p.calls)


async def test_one_failing_task_keeps_the_others():
    class Flaky(FakeProvider):
        async def generate_json(self, **kw):
            if "игроков" in kw["prompt"]:
                raise ValueError("boom")
            return await super().generate_json(**kw)

    res = await an.analyze_game(Flaky(), game(), reviews("critic", 5), reviews("user", 20), "m")
    assert res["user"].error and res["critic"].data and res["essence"].data
