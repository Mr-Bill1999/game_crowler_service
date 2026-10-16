import numpy as np

from app.ai.light_essence import clean_tags, flat_tags, source_hash
from app.models import Game
from app.pipeline.similarity import Item, neighbours, tag_matrix


def vec(*hot: int, dim: int = 8) -> np.ndarray:
    v = np.full(dim, 0.05, dtype=np.float32)
    for h in hot:
        v[h] = 1
    return v


def catalogue() -> list[Item]:
    # query 1 plays like 2 (gameplay), lives in the world of 3, shares rare tags with 4; 5..8 are unrelated
    items = [
        Item(1, vec(0), vec(0), ["mechanics:stealth", "mechanics:loot", "tone:dark"]),
        Item(2, vec(0), vec(5), ["tone:cozy"]),
        Item(3, vec(3), vec(0), ["tone:cozy"]),
        Item(4, vec(4), vec(6), ["mechanics:stealth", "mechanics:loot"]),
    ]
    # fillers never touch dimension 0, where the query's gameplay and world live
    items += [Item(i, vec(1 + i % 7, 1 + (i + 3) % 7), vec(1 + (i + 1) % 7, 1 + (i + 5) % 7), ["tone:cozy"])
              for i in range(5, 12)]
    return items


def test_gameplay_outweighs_tags_outweighs_world():
    top = [x["other_id"] for x in neighbours(catalogue())[1]["similar"][:3]]
    assert top == [2, 4, 3]


def test_reasons_list_shared_tags_rarest_first():
    first = {x["other_id"]: x for x in neighbours(catalogue())[1]["similar"]}
    assert set(first[4]["reasons"]["tags"]) == {"mechanics:stealth", "mechanics:loot"}
    assert first[2]["reasons"]["signals"]["gameplay"] > 1


def test_same_series_goes_to_its_own_block():
    items = catalogue()
    items[0].series, items[1].series = "Thief", "thief!"
    out = neighbours(items)[1]
    assert [x["other_id"] for x in out["series"]] == [2]
    assert 2 not in [x["other_id"] for x in out["similar"]]


def test_rare_tags_weigh_more():
    _, vocab, weight = tag_matrix([["tone:cozy", "mechanics:stealth"]] + [["tone:cozy"]] * 9)
    w = dict(zip(vocab, weight, strict=True))
    assert w["mechanics:stealth"] > w["tone:cozy"]


def test_tags_are_cleaned_and_flattened():
    e = clean_tags({"mechanics": ["stealth", "stealth", "made-up", "loot"], "perspective": ["top-down", "isometric"],
                    "modes": [], "setting": None, "tone": ["dark"]})
    assert e["mechanics"] == ["stealth", "loot"] and e["perspective"] == ["top-down"] and e["setting"] == []
    assert flat_tags(e) == ["mechanics:stealth", "mechanics:loot", "perspective:top-down", "tone:dark"]


def test_essence_is_rebuilt_only_when_its_sources_change():
    g = Game(title="X", genres=["Action"], description="Fight  things.")
    same = Game(title="X", genres=["Action"], description="Fight things. ")
    assert source_hash(g) == source_hash(same)
    assert source_hash(g) != source_hash(Game(title="X", genres=["Action"], description="Build things."))


def test_floor_leaves_out_games_that_do_not_play_alike():
    items = catalogue()
    loose = [x["other_id"] for x in neighbours(items)[1]["similar"]]
    strict = [x["other_id"] for x in neighbours(items, min_gameplay=0.5)[1]["similar"]]
    assert len(strict) < len(loose) and strict[0] == 2  # the one that plays alike stays
    assert all(x["reasons"]["gameplay"] >= 0.5 for x in neighbours(items, min_gameplay=0.5)[1]["similar"])
