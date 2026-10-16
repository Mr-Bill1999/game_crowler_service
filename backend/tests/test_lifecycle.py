from datetime import UTC, date, datetime, timedelta

from app.ai import lifecycle as lc
from app.ai.base import LLMResult
from app.models import Analysis, Game, Review

T0 = datetime(2026, 10, 10, tzinfo=UTC)
RU = "Разбор на русском языке о бое и сюжете игры."


class FakeProvider:
    name = "fake"

    def __init__(self, replies=None):
        self.prompts: list[tuple[str, str]] = []
        self.replies = list(replies or [])

    async def generate_json(self, *, system, prompt, schema, model=None, thinking_level=None):
        self.prompts.append((system, prompt))
        summary = self.replies.pop(0) if self.replies else RU
        data = {"summary": summary, "consensus": "positive", "liked": ["a"], "disliked": ["b"], "notable_quotes": []}
        return LLMResult(data, model or "m", 1000, 100, 0.001)


def game() -> Game:
    return Game(id=7, slug="g", title="Game", genres=[], critic_count=4, user_count=40, platforms=[],
                release_date=date(2026, 10, 1), metascore=80)


def user(i: int, words: int, fetched: datetime, score: float = 8) -> Review:
    return Review(id=f"u{i}", game_id=7, kind="user", score=score, date=date(2026, 10, 2), platform="PC",
                  quote=" ".join(f"w{i}_{k}" for k in range(words)), fetched_at=fetched)


async def test_first_read_samples_substantive_reviews_with_stats():
    reviews = [user(i, 60, T0) for i in range(10)] + [user(100 + i, 8, T0, score=0) for i in range(30)]
    p = FakeProvider()
    run = await lc.analyze_kind(p, game(), "user", reviews, None, "m", None)
    assert run.mode == "first" and run.read == 10 and run.texts_seen == 10
    system, prompt = p.prompts[0]
    assert system == lc.SYSTEM
    assert "Распределение оценок всех 40 отзывов" in prompt  # short reviews counted in the stats...
    assert "w100_0" not in prompt  # ...but not sent as text
    assert run.watermark == T0


async def test_update_reads_only_reviews_after_the_watermark():
    old = [user(i, 60, T0) for i in range(10)]
    new = [user(50 + i, 60, T0 + timedelta(days=1)) for i in range(3)]
    prev = Analysis(kind="user", payload={"summary": "старый разбор"}, watermark=T0)
    p = FakeProvider()
    run = await lc.analyze_kind(p, game(), "user", old + new, prev, "m", None)
    system, prompt = p.prompts[0]
    assert run.mode == "update" and run.read == 3 and system == lc.UPDATE_SYSTEM
    assert "старый разбор" in prompt and "w50_0" in prompt and "w0_0" not in prompt
    assert run.watermark == T0 + timedelta(days=1)


async def test_only_short_new_reviews_skip_the_model_but_move_the_watermark():
    reviews = [user(i, 60, T0) for i in range(10)] + [user(99, 5, T0 + timedelta(hours=3))]
    prev = Analysis(kind="user", payload={}, watermark=T0)
    p = FakeProvider()
    run = await lc.analyze_kind(p, game(), "user", reviews, prev, "m", None)
    assert run.mode == "skip" and not p.prompts and run.watermark == T0 + timedelta(hours=3)


async def test_answer_in_another_language_is_retried_once():
    p = FakeProvider(replies=["An English summary of the reviews.", RU])
    run = await lc.analyze_kind(p, game(), "user", [user(i, 60, T0) for i in range(6)], None, "m", None)
    assert len(run.calls) == 2 and run.data["summary"] == RU
    assert lc.RUSSIAN_REMINDER.strip() in p.prompts[1][1]


async def test_critic_scores_are_banded_on_the_100_scale():
    critics = [Review(id=f"c{i}", game_id=7, kind="critic", score=s, quote="ok", date=date(2026, 10, 2),
                      fetched_at=T0, publication="P") for i, s in enumerate([90, 85, 40])]
    p = FakeProvider()
    run = await lc.analyze_kind(p, game(), "critic", critics, None, "m", None)
    assert run.mode == "first"
    assert "80–100: 2 (67%)" in p.prompts[0][1] and "0–49: 1 (33%)" in p.prompts[0][1]
