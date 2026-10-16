from app.ai.deep import absolute_facts, clock
from app.sources.youtube import (
    Clip,
    Video,
    is_silent,
    parse_chapters,
    parse_duration,
    pick_clips,
    pick_video,
    title_matches,
)


def vid(title: str, views: int = 1000, minutes: int = 40, **kw) -> Video:
    return Video(id=title[:8], title=title, channel="c", views=views, duration_s=minutes * 60, **kw)


def test_durations():
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT45M") == 2700
    assert parse_duration("P1DT2H") == 26 * 3600
    assert parse_duration("") == 0


def test_chapters_follow_youtube_rules():
    desc = "Intro\n0:00 Intro\n02:15 - First fight\n1:05:00 Boss\nnot a chapter 3:00"
    assert parse_chapters(desc, 2 * 3600) == [(0, "Intro"), (135, "First fight"), (3900, "Boss")]
    assert parse_chapters("1:00 a\n2:00 b\n3:00 c", 3600) == []  # must start at 0:00


def test_title_match_ignores_apostrophes_and_edition_words():
    assert title_matches("Marvel's Wolverine", "MARVEL'S WOLVERINE PS5 Walkthrough Part 1")
    assert title_matches("Toy Story 3 Complete Edition", "Toy Story 3 gameplay part 1")
    assert not title_matches("Control Resonant", "Control Ultimate Edition walkthrough")


def test_title_match_on_the_boltgun_2_search():
    t = "Warhammer 40,000: Boltgun 2"
    assert title_matches(t, "Warhammer 40k: Boltgun 2 Is More of a Good Thing")  # 40k = 40,000
    assert title_matches(t, "BOLTGUN 2 Gameplay Walkthrough FULL DEMO")  # the franchise is optional
    assert title_matches(t, "WH40k: Boltgun 2 - Let's Play")
    assert not title_matches(t, "WARHAMMER 40K Boltgun | Full Game Walkthrough")  # the first game: no "2"
    assert title_matches("Disney/Pixar Toy Story 3 Complete Edition", "Toy Story 3 Complete Edition Walkthrough")
    assert not title_matches("Disney/Pixar Toy Story 3 Complete Edition", "Toy Story 2 Walkthrough")
    assert title_matches("Resident Evil II: Deluxe Edition", "Resident Evil 2 Walkthrough")  # roman numeral


def pick(videos, released=True):
    return pick_video("Marvel's Wolverine", videos, min_views=1000, max_s=12 * 3600, released=released)


def test_pick_video_prefers_watchable_lets_plays():
    videos = [
        vid("Marvel's Wolverine - Official Trailer", views=9_000_000, minutes=12),
        vid("Marvel's Wolverine Full Game 4K", views=800_000, minutes=600),
        vid("Marvel's Wolverine Walkthrough Part 1", views=200_000, minutes=55),
        vid("Spider-Man 2 walkthrough", views=5_000_000),
    ]
    assert pick(videos).duration_s == 600 * 60  # long full playthroughs are fine now: we watch 20 minutes of them
    assert pick(videos[:1]) is None


def test_demos_low_views_and_silence():
    demo = vid("Marvel's Wolverine FULL DEMO walkthrough", views=50_000)
    tiny = vid("Marvel's Wolverine Lets Play Part 1", views=300)
    assert pick([demo, tiny]) is None  # a demo of a released game, an upload nobody watched: no let's play yet
    assert pick([demo], released=False) is demo  # before release the demo is what there is
    silent = vid("Marvel's Wolverine Walkthrough No Commentary", views=900_000)
    talky = vid("Marvel's Wolverine Let's Play Part 1", views=40_000)
    assert pick([silent, talky]) is talky


def test_clips_take_about_twenty_minutes_spread_over_the_video():
    hour = pick_clips(vid("x", minutes=60))
    assert len(hour) == 3 and all(c.length == 390 for c in hour) and sum(c.length for c in hour) == 1170
    long = pick_clips(vid("x", minutes=300))
    assert len(long) == 4 and all(c.length == 300 for c in long)
    assert long[0].start_s > 600 and long[-1].start_s > 4 * 3600  # from the first quarter to the last
    short = pick_clips(vid("x", minutes=12))
    assert all(c.length == 180 for c in short) and short[-1].end_s <= 720


def test_clips_follow_chapters_when_there_are_enough():
    chapters = [(0, "Intro"), (300, "A"), (900, "B"), (1500, "C"), (2100, "D"), (2700, "E")]
    clips = pick_clips(vid("x", minutes=60, chapters=chapters))
    assert [(c.start_s, c.label) for c in clips] == [(300, "A"), (1500, "C"), (2700, "E")]


def test_fact_timecodes_become_absolute_links():
    v = vid("x", minutes=60)
    clips = [Clip(600, 900), Clip(1800, 2100)]
    out = absolute_facts({"facts": [
        {"text": "video time", "clip": 2, "at": "31:30"},
        {"text": "clip time", "clip": 2, "at": "1:30"},
        {"text": "garbage", "clip": 9, "at": "x"},
        {"text": "outside the clip", "clip": 1, "at": "50:00"},
    ]}, v, clips)
    t = {f["text"]: f for f in out}
    assert t["video time"]["t"] == 1890 and t["video time"]["at"] == clock(1890)
    assert t["video time"]["url"].endswith("&t=1890s")
    assert t["clip time"]["t"] == 1890
    assert t["garbage"]["t"] == 1800  # bad clip number and time fall back to the start of the nearest clip
    assert t["outside the clip"]["t"] == 600
    assert [f["t"] for f in out] == sorted(f["t"] for f in out)  # listed in viewing order




def test_silence_is_read_from_title_and_description():
    assert is_silent(vid("Прохождение без комментариев Castlevania"))
    assert not is_silent(vid("Castlevania Let's Play Part 1"))


def test_a_titled_lets_play_beats_an_essay_with_more_views():
    essay = vid("Marvel's Wolverine Is More of a Good Thing", views=239_000, minutes=21)
    silent = vid("Marvel's Wolverine Full Walkthrough No Commentary", views=6_600, minutes=248)
    assert pick([essay, silent]) is silent
    assert pick([essay]) is essay  # an essay is still better than nothing


def test_streams_are_never_taken():
    titled = vid("STREAM ENDS WHEN I BEAT MARVEL'S WOLVERINE", views=40_000, minutes=300)
    red = vid("🔴 Marvel's Wolverine Let's Play Part 1", views=40_000)
    archived = vid("Marvel's Wolverine Walkthrough Part 1", views=90_000, was_live=True)  # a broadcast, quietly titled
    russian = vid("Marvel's Wolverine стрим прохождение", views=90_000)
    assert pick([titled, red, archived, russian]) is None
    assert not pick([vid("Marvel's Wolverine Livestream recap", views=5000)])
    walkthrough = vid("Marvel's Wolverine Walkthrough Part 1", views=5_000)
    assert pick([titled, archived, walkthrough]) is walkthrough
