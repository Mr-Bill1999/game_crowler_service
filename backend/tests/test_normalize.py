import json
from datetime import date
from pathlib import Path

from app.scraper.normalize import (
    critic_review_id,
    parse_composer,
    parse_finder_item,
    parse_review,
)

FIX = Path(__file__).parent / "fixtures"
IMG = "https://www.metacritic.com/a/img/catalog"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_composer_card_fields():
    card = parse_composer(load("composer_elden-ring.json"), IMG)
    assert card.id == 1300501979
    assert card.slug == "elden-ring"
    assert card.title == "Elden Ring"
    assert card.release_date == date(2022, 2, 25)
    assert card.developer == "From Software"
    assert "Bandai Namco Games" in card.publisher
    assert card.genres == ["Action RPG"]
    assert card.metascore == 96 and card.critic_count > 0
    assert card.userscore == 8.4 and card.user_count == 24832
    assert card.cover_url.startswith(IMG) and card.cover_url.endswith(".jpg")
    assert card.video_embed_url.startswith("https://cdn.jwplayer.com/")
    assert card.description.startswith("A New World")


def test_composer_platforms_carry_their_own_scores():
    card = parse_composer(load("composer_elden-ring.json"), IMG)
    by_slug = {p.slug: p for p in card.platforms}
    assert set(by_slug) == {"xbox-one", "pc", "playstation-4", "xbox-series-x", "playstation-5"}
    assert (by_slug["pc"].metascore, by_slug["pc"].critic_count) == (94, 63)
    assert by_slug["xbox-one"].critic_count is None


def test_critic_review_ids_are_stable_and_unique():
    items = load("critic_elden-ring.json")["data"]["items"]
    ids = [critic_review_id(r) for r in items]
    assert len(set(ids)) == len(ids)
    assert ids == [critic_review_id(dict(r)) for r in items]
    assert all(i.startswith("c:") for i in ids)


def test_review_parsing():
    critic = parse_review(load("critic_elden-ring.json")["data"]["items"][0], "critic")
    assert critic.publication == "Areajugones" and critic.score == 100 and critic.platform == "PlayStation 5"
    assert critic.url.startswith("https://")

    user = parse_review(load("user_elden-ring.json")["data"]["items"][0], "user")
    assert user.id == "u:b4d603b2-7d56-49ec-a2c7-a4b7ace06b98"
    assert user.publication is None and user.date == date(2026, 10, 13)


def test_finder_item():
    raw = load("finder_catalog.json")["data"]["items"][0]
    item = parse_finder_item(raw)
    assert item.id == raw["id"] and item.slug == raw["slug"]
    assert item.release_date is not None


def test_scores_without_any_ratings_are_empty_not_zero():
    raw = load("composer_elden-ring.json")
    for comp in raw["components"]:
        if comp["meta"]["componentName"] == "user-score-summary":
            comp["data"]["item"].update(score=0, reviewCount=0)
    card = parse_composer(raw, IMG)
    assert card.userscore is None and card.user_count == 0
