from app.ai import checks
from app.ai.lifecycle import russian_share

REVIEW = ("Faithfully capturing Clive Barker's grotesque, flesh-and-iron body horror aesthetic alongside spatial "
          "Lament Configuration puzzles, it delivers an undeniably unsettling experience. The combat is stiff.")


def test_verbatim_quote_is_kept():
    q = "The combat is stiff."
    assert checks.snap_quote(q, [REVIEW]) == q


def test_lightly_edited_quote_becomes_the_reviews_own_words():
    q = "A faithful capturing Clive Barker's grotesque, flesh-and-iron body horror aesthetic alongside spatial"
    snapped = checks.snap_quote(q, [REVIEW])
    assert snapped and snapped.startswith("Faithfully capturing") and snapped in REVIEW


def test_made_up_or_stitched_quote_is_dropped():
    assert checks.snap_quote("Wolverine is the easiest slam dunk... but the game plays itself", [REVIEW]) is None
    out = checks.finish({"summary": "x", "notable_quotes": ["Nothing like this was said anywhere at all", "The combat is stiff."]},
                        title="Hellraiser", texts=[REVIEW])
    assert out["notable_quotes"] == ["The combat is stiff."]


def test_misspelled_title_is_found_and_replaced():
    data = {"summary": "Игроки тепло приняли Order of the Sinking State, хвалят головоломки и звуковое оформление.",
            "liked": ["Order of the Sinking State хорош"]}
    found = checks.problems(data, title="Order of the Sinking Star", stats="", russian_share=russian_share)
    assert [p.kind for p in found] == ["title"]
    fixed = checks.finish(data, title="Order of the Sinking Star", texts=[])
    assert "Sinking Star," in fixed["summary"] and fixed["liked"] == ["Order of the Sinking Star хорош"]


def test_percentages_must_come_from_the_statistics():
    stats = "- позитивные 8–10: 19 (76%)\\nВсе оценки Metacritic: позитивные 54 (79%)"
    ok = {"summary": "76% игроков и 79% всех оценок положительные"}
    bad = {"summary": "76% игроков и 83% критиков оценили игру высоко"}
    assert checks.problems(ok, title="X", stats=stats, russian_share=russian_share) == []
    assert [p.detail for p in checks.problems(bad, title="X", stats=stats, russian_share=russian_share)] == [
        "83% нет в переданной статистике"]
