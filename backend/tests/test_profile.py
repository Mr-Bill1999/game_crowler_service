"""Game profile: fullness, Steam parsing and sampling, and the checks that drop references the model made up."""

from app.profile import build
from app.profile.evidence import Doc, accept, batches
from app.profile.fullness import Point, band, factor_score, fullness, missing
from app.sources.steam import SteamReview, parse_news, parse_reviews, parse_store, same_developer, sample


def pt(factor, source="steam_reviews", specific=True, polarity="plus", who=None, is_claim=False):
    return Point(factor, source, specific, polarity, who, is_claim)


# ---------- fullness ----------

def test_three_concrete_points_from_two_kinds_of_source_fill_a_factor():
    points = [pt(3), pt(3), pt(3, source="critic_full")]
    assert factor_score(points, 3)["score"] == 1.0


def test_generic_claims_do_not_count_and_developer_words_count_a_third():
    assert factor_score([pt(2, specific=False)] * 5, 2)["score"] == 0
    store = factor_score([pt(2, source="steam_store", is_claim=True)] * 3, 2)
    assert store["points"] == 1.0 and store["kinds"] == []  # not a confirmation: no corroboration either


def test_factor_five_needs_weaknesses_or_who_it_fails_for():
    assert factor_score([pt(5, polarity="plus")] * 4, 5)["points"] == 0
    assert factor_score([pt(5, polarity="minus"), pt(5, polarity="plus", who="новички")], 5)["points"] == 1.5


def test_fullness_bands_and_empty_game():
    assert fullness([])[0] == 0
    assert band(0) == ("thin", "скудно") and band(59)[0] == "partial" and band(60)[0] == "enough"
    assert band(80) == ("rich", "богато")


def test_missing_names_sources_first_then_weak_factors():
    factors = {str(f): {"score": 1.0 if f != 4 else 0.2} for f in range(1, 7)}
    out = missing(factors, {"steam_reviews": 7}, steam_linked=True, critic_links=0)
    assert out[0].startswith("Мало отзывов в Steam: 7")
    assert "Нет рецензий критиков" in out and "эмоциональное предложение" in out[-1]
    assert missing(factors, {}, steam_linked=False, critic_links=3)[0].startswith("Игры нет в Steam")


# ---------- Steam ----------

def review(i, voted_up=True, hours=1.0):
    return SteamReview(str(i), "text", voted_up, hours, "english", False, None)


def test_sample_takes_every_recommend_by_hours_cell_in_turn():
    reviews = [review(i, True, 1) for i in range(10)] + [review(100, False, 60), review(101, True, 20)]
    got = sample(reviews, 3)
    assert {r.id for r in got} == {"0", "100", "101"}  # the rare cells are not crowded out by the common one
    assert len(sample(reviews, 50)) == 12


def test_parse_reviews_keeps_hours_and_drops_short_texts():
    data = {"reviews": [
        {"recommendationid": "1", "review": "word " * 20, "voted_up": False, "language": "english",
         "author": {"playtime_at_review": 150}, "timestamp_created": 1760000000},
        {"recommendationid": "2", "review": "great", "voted_up": True, "author": {}},
    ]}
    got = parse_reviews(data, min_words=15)
    assert [(r.id, r.voted_up, r.hours) for r in got] == [("1", False, 2.5)]


def test_parse_store_reads_tags_from_the_page_and_cleans_html():
    data = {"42": {"data": {"name": "Hubert", "developers": ["Brocap"], "about_the_game": "<p>A dog</p><br>walks",
                            "genres": [{"description": "Adventure"}], "categories": [], "release_date": {"date": "x"}}}}
    page = '<a class="app_tag" href="#">\n Dogs </a><a class="app_tag">Relaxing</a>'
    s = parse_store(42, data, page)
    assert s.tags == ["Dogs", "Relaxing"] and s.about == "A dog\nwalks" and "Метки игроков Steam: Dogs" in s.meta


def test_news_keeps_only_the_developers_own_posts():
    long = "patch " * 50
    data = {"appnews": {"newsitems": [{"gid": "1", "feed_type": 1, "title": "1.0.2", "contents": long, "date": 1},
                                      {"gid": "2", "feed_type": 0, "title": "press", "contents": long}]}}
    assert [n["gid"] for n in parse_news(data)] == ["1"]


def test_same_developer_tolerates_legal_suffixes_only():
    assert same_developer(["Keen Games GmbH"], "Keen Games")
    assert not same_developer(["Other Studio"], "Keen Games")
    assert not same_developer(["Keen Games"], None)


# ---------- references the model made up ----------

def test_evidence_maps_prompt_numbers_to_documents_and_drops_unknown_ones():
    batch = [(0, Doc(501, "user_review", "t", {})), (1, Doc(502, "user_review", "t", {}))]
    row, bad = accept({"factor": 3, "claim": "c", "docs": [1, 7, 1], "specific": True, "polarity": "plus",
                       "source_is_claim": False}, batch, "steam_reviews")
    assert row["document_ids"] == [502] and bad == 1
    row, _ = accept({"factor": 3, "claim": "c", "docs": [9], "specific": True, "polarity": "plus",
                     "source_is_claim": False}, batch, "steam_reviews")
    assert row is None  # a claim with no real document behind it is not evidence


def test_store_and_news_evidence_is_always_the_developers_claim():
    batch = [(0, Doc(1, "store_text", "t", {}))]
    row, _ = accept({"factor": 1, "claim": "c", "docs": [0], "specific": True, "polarity": "neutral",
                     "source_is_claim": False}, batch, "steam_store")
    assert row["is_claim"] is True


def test_batches_split_long_sources():
    docs = [Doc(i, "user_review", "x" * 20_000, {}) for i in range(7)]
    assert [len(b) for b in batches(docs)] == [2, 2, 2, 1]


def test_profile_points_keep_only_evidence_that_exists():
    data = {"factors": [{"factor": 2, "summary": "s", "confidence": "high", "points": [
        {"text": "a", "evidence": ["E1", "E99"]}, {"text": "b", "evidence": ["E77"]}]}]}
    payload, dropped = build.clean(data, known={1, 2})
    assert payload["2"]["points"] == [{"text": "a", "evidence": [1]}] and dropped == 2
