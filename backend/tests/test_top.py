from app.core.config import Settings
from app.pipeline.top import top_changes
from app.scraper.metacritic import Metacritic


def test_top_changes_lists_entries_and_exits():
    assert top_changes(["a", "b", "c"], ["d", "a", "b"]) == {"entered": ["d"], "left": ["c"]}
    assert top_changes([], ["a"]) == {"entered": ["a"], "left": []}


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def get_json(self, stage, path, params=None, slug=None):
        self.calls.append((stage, path, params))
        items = [{"id": i, "slug": f"g{i}", "title": "", "releaseDate": "2026-10-15", "platforms": [],
                  "criticScoreSummary": {"score": 80}} for i in range(20)]
        return {"data": {"items": items, "totalResults": 18626}}


async def test_top_uses_the_same_query_as_the_site():
    c = RecordingClient()
    top = await Metacritic(c, Settings()).new_releases_top()
    stage, path, params = c.calls[0]
    assert stage == "discovery.top" and path == "/finder/metacritic/web"
    assert params == {"sortBy": "-releaseDate", "metaScoreMin": 1, "offset": 0, "limit": 20, "mcoTypeId": 13}
    assert [i.slug for i in top][:2] == ["g0", "g1"] and len(top) == 20
