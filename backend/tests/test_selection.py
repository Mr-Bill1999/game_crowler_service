from collections import Counter
from datetime import date

from app.ai.selection import band, score_stats, stratified_sample, substantive
from app.models import Review


def rev(i: int, score: float, words: int, text: str | None = None) -> Review:
    return Review(id=f"u{i}", game_id=1, kind="user", score=score, date=date(2026, 10, 1),
                  quote=text or " ".join(f"w{i}x{k}" for k in range(words)))


def test_short_reviews_and_duplicates_are_dropped():
    same = " ".join(["great combat story graphics"] * 15)
    rs = [rev(1, 10, 10), rev(2, 0, 60), rev(3, 9, 0, same), rev(4, 9, 0, same.upper())]
    kept = substantive(rs)
    assert [r.id for r in kept] == ["u2", "u3"]


def test_sample_keeps_score_proportions_of_all_reviews():
    population = [rev(i, 10, 10) for i in range(600)] + [rev(1000 + i, 1, 10) for i in range(400)]
    long_ones = [rev(2000 + i, 10, 80) for i in range(500)] + [rev(3000 + i, 1, 80) for i in range(500)]
    sample = stratified_sample(long_ones, population, cap=100)
    bands = Counter(band(r.score) for r in sample)
    assert len(sample) == 100
    assert bands["позитивные 8–10"] == 60 and bands["негативные 0–4"] == 40


def test_small_sets_are_not_sampled():
    rs = [rev(i, 8, 80) for i in range(20)]
    assert stratified_sample(rs, rs, cap=300) == rs


def test_stats_cover_short_reviews_too():
    text = score_stats([rev(1, 10, 5), rev(2, 0, 80), rev(3, 6, 80)],
                       {"reviewCount": 900, "positiveCount": 500, "neutralCount": 100, "negativeCount": 300})
    assert "всех 3 отзывов" in text and "33%" in text and "900" in text


def test_retry_delay_follows_google_retry_info():
    from app.ai.gemini import retry_delay

    err = Exception("429 RESOURCE_EXHAUSTED {'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '37s'}]}")
    assert 38 <= retry_delay(err, 1) < 40
    assert retry_delay(Exception("503 overloaded"), 1) < 4


def test_critic_bands_follow_metacritic_not_scaled_player_bands():
    from app.ai.selection import critic_stats
    crit = [Review(id=f"c{i}", game_id=1, kind="critic", score=s, quote="x") for i, s in enumerate((75, 77, 74, 49, 90))]
    assert band(75, 0.1) == "позитивные 75–100" and band(74, 0.1) == "смешанные 50–74" and band(49, 0.1) == "негативные 0–49"
    text = critic_stats(crit, 77)
    assert "позитивные 75–100: 3 (60%)" in text and "смешанные 50–74: 1 (20%)" in text
