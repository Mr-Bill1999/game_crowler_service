from app.ai.light_essence import TAG_GROUPS, TAG_WORDS, finish_essence, series_in_title


def base(**kw):
    e = {"genre": "", "gameplay": "", "world": "", "mechanics": [], "perspective": [], "modes": [], "setting": [],
         "tone": [], "series": "", "comparable_to": [], "confidence": "high"}
    e.update(kw)
    return e


def test_every_finisher_tag_is_in_the_vocabulary():
    for tag in TAG_WORDS:
        group, value = tag.split(":")
        assert value in TAG_GROUPS[group][0], tag


def test_series_found_in_the_title_fills_a_gap_and_shortens_a_variant():
    known = ["Toy Story", "Castlevania", "Warhammer 40K"]
    assert series_in_title("Disney/Pixar Toy Story: Retro Roundup!", known) == "Toy Story"
    assert series_in_title("Warhammer 40,000: Boltgun 2", known) == "Warhammer 40K"  # 40,000 and 40K are one number
    assert series_in_title("Story of Seasons", known) is None
    e = finish_essence(base(), title="Disney/Pixar Toy Story: Retro Roundup!", genres=[], description="",
                       known_series=known)
    assert e["series"] == "Toy Story" and "series:Toy Story" in e["_finished"]
    e = finish_essence(base(series="Disney/Pixar Toy Story"), title="Disney/Pixar Toy Story 3", genres=[],
                       description="", known_series=known)
    assert e["series"] == "Toy Story"


def test_tags_the_texts_state_outright():
    e = finish_essence(base(genre="экшен-платформер метроидвания", mechanics=["melee-combat"]),
                       title="Castlevania: Belmont's Curse", genres=["Metroidvania"], description="", known_series=[])
    assert e["mechanics"] == ["melee-combat", "metroidvania", "platforming"]
    e = finish_essence(base(), title="X", genres=[], description="Published by Embracer Group.", known_series=[])
    assert "racing" not in e["mechanics"]  # words match from their start only
