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


def test_pick_video_prefers_watchable_lets_plays():
    videos = [
        vid("Marvel's Wolverine - Official Trailer", views=9_000_000, minutes=12),
        vid("Marvel's Wolverine Full Game 4K", views=800_000, minutes=600),
        vid("Marvel's Wolverine Walkthrough Part 1 (3h)", views=500_000, minutes=170),
        vid("Marvel's Wolverine Walkthrough Part 1", views=200_000, minutes=55),
        vid("Spider-Man 2 walkthrough", views=5_000_000),
    ]
    assert pick_video("Marvel's Wolverine", videos, 3600, 3 * 3600).duration_s == 55 * 60
    assert pick_video("Marvel's Wolverine", videos[:3], 3600, 3 * 3600).duration_s == 170 * 60
    assert pick_video("Marvel's Wolverine", videos[:1], 3600, 3 * 3600) is None


def test_clips_spread_over_the_video_and_skip_the_intro():
    clips = pick_clips(vid("x", minutes=60), 3, 300)
    assert [(c.start_s, c.end_s) for c in clips] == [(360, 660), (1620, 1920), (2880, 3180)]
    short = pick_clips(vid("x", minutes=12), 3, 300)
    assert all(c.length == 180 for c in short) and short[-1].end_s <= 720


def test_clips_follow_chapters_when_there_are_enough():
    chapters = [(0, "Intro"), (300, "A"), (900, "B"), (1500, "C"), (2100, "D"), (2700, "E")]
    clips = pick_clips(vid("x", minutes=60, chapters=chapters), 3, 300)
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


def test_commentary_beats_views_and_length():
    silent = vid("Castlevania Belmont's Curse Walkthrough Part 1 (PS5 Pro) No Commentary", views=900_000, minutes=30)
    talky = vid("Castlevania Belmont's Curse - Let's Play Part 1", views=40_000, minutes=95)
    assert is_silent(silent) and not is_silent(talky)
    assert pick_video("Castlevania: Belmont's Curse", [silent, talky], 3600, 3 * 3600) is talky
    assert pick_video("Castlevania: Belmont's Curse", [silent], 3600, 3 * 3600) is silent  # better than nothing
    assert is_silent(vid("Прохождение без комментариев Castlevania"))
