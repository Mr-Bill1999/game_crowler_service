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
    assert "по-русски" in p.prompts[1][1]


async def test_critic_scores_are_banded_on_the_100_scale():
    critics = [Review(id=f"c{i}", game_id=7, kind="critic", score=s, quote="ok", date=date(2026, 10, 2),
                      fetched_at=T0, publication="P") for i, s in enumerate([90, 85, 40])]
    p = FakeProvider()
    run = await lc.analyze_kind(p, game(), "critic", critics, None, "m", None)
    assert run.mode == "first"
    assert "75–100: 2 (67%)" in p.prompts[0][1] and "0–49: 1 (33%)" in p.prompts[0][1]


async def test_title_typos_and_talk_of_the_sample_get_one_retry_then_are_repaired():
    bad = "Иггроки хвалят Gamе Sequel: в нашей выборке 96% довольны."  # Cyrillic "е" in the title
    p = FakeProvider(replies=[bad, "Игроки хвалят Gamе Sequel."])
    g = game()
    g.title = "Game Sequel"
    run = await lc.analyze_kind(p, g, "user", [user(i, 60, T0) for i in range(6)], None, "m", None)
    assert len(run.calls) == 2 and "Название игры пиши точно" in p.prompts[1][1] and "выборку" in p.prompts[1][1]
    assert run.data["summary"] == "Игроки хвалят Game Sequel." and "_checks" not in run.data


def kind_run(kind: str, texts: int, data: bool = True, error: str | None = None) -> lc.KindRun:
    return lc.KindRun(kind, "error" if error else ("update" if data else "skip"), data={"summary": RU} if data else None,
                      texts_seen=texts, error=error)


def test_a_failed_part_does_not_make_the_next_hour_redo_the_parts_that_succeeded():
    """Nov 2026: one game's player summary ran 7 times in a day because another part kept failing."""
    prev = {"critic": Analysis(kind="critic", texts_seen=40), "user": Analysis(kind="user", texts_seen=500)}
    runs = [kind_run("critic", 45, data=False, error="503"), kind_run("user", 620)]
    assert lc.checked(runs, essence_failed=False)  # the player summary was written: today's check is done
    assert lc.covered_texts(runs, prev) == 40 + 620  # the critic texts stay unread, so they count as new tomorrow
    assert lc.checked([kind_run("critic", 45), kind_run("user", 620)], essence_failed=True)
    assert not lc.checked([kind_run("critic", 45, data=False, error="503"),
                           kind_run("user", 620, data=False, error="503")], essence_failed=False)  # retry next hour
    assert lc.checked([kind_run("critic", 45, data=False), kind_run("user", 620, data=False)], essence_failed=False)
    assert lc.covered_texts([kind_run("critic", 3, data=False, error="x")], {}) == 0


def test_an_essence_older_than_a_summary_is_built_again():
    a = lambda kind, h: Analysis(kind=kind, created_at=T0 + timedelta(hours=h))
    assert lc.essence_stale({"user": a("user", 1)})  # never built
    assert not lc.essence_stale({})
    assert not lc.essence_stale({"user": a("user", 1), "essence": a("essence", 1)})  # written together
    assert lc.essence_stale({"critic": a("critic", 1), "user": a("user", 5), "essence": a("essence", 1)})


def test_a_saturated_kind_sits_out_the_run_the_other_kind_started():
    prev = Analysis(kind="user", texts_seen=300)
    assert lc.held("user", [user(i, 60, T0) for i in range(449)], prev, 300, 0.5)
    assert not lc.held("user", [user(i, 60, T0) for i in range(450)], prev, 300, 0.5)
    assert not lc.held("user", [user(i, 60, T0) for i in range(10)], Analysis(kind="user", texts_seen=299), 300, 0.5)
    assert not lc.held("critic", [], None, 300, 0.5)
