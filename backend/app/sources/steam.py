"""Steam: find the game, its store page, player reviews and the developer's news. Public endpoints, no key.

Reviews are the source that decides whether a game has enough to profile (docs/game-profile.md): they carry the
player's hours and whether they recommend the game, which is what "for whom it stops working" stands on. They are
sampled across recommend x hours cells, so a hundred reviews cover quitters and veterans, fans and the disappointed.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.sources.web import WebClient

STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"
COOKIES = {"birthtime": "568022401", "mature_content": "1", "wants_mature_content": "1"}  # past the age gate
HOUR_BANDS = (2, 10, 50)  # < 2 h: the refund window; 2-10; 10-50; 50+


def clean(s: str | None) -> str:
    """Store HTML and Steam BBCode to plain text."""
    s = re.sub(r"<br\s*/?>|</p>|</li>|\[/?(?:p|list|\*|h\d)\]", "\n", s or "", flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>|\[/?[a-z0-9]+(?:=[^\]]*)?\]", " ", s, flags=re.IGNORECASE)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("&", "and").replace("®", "").replace("™", "")).strip()


def words(s: str) -> int:
    return len(re.findall(r"\w+", s or ""))


def same_developer(steam_devs: list[str], mc_developer: str | None) -> bool:
    ours = [norm(x) for x in (mc_developer or "").split(",")]
    return any(a and b and (a in b or b in a) for a in (norm(x) for x in steam_devs) for b in ours)


@dataclass
class StorePage:
    appid: int
    name: str
    developers: list[str]
    about: str
    meta: str
    release: str | None
    tags: list[str] = field(default_factory=list)


@dataclass
class SteamReview:
    id: str
    text: str
    voted_up: bool
    hours: float
    lang: str | None
    early_access: bool
    posted_at: datetime | None

    @property
    def cell(self) -> tuple[bool, int]:
        return self.voted_up, sum(self.hours >= b for b in HOUR_BANDS)


def parse_store(appid: int, data: dict, page_html: str) -> StorePage:
    d = (data.get(str(appid)) or {}).get("data") or {}
    tags = [clean(t) for t in re.findall(r'class="app_tag"[^>]*>\s*([^<]+?)\s*<', page_html or "")]
    meta = [f"Жанры Steam: {', '.join(x['description'] for x in d.get('genres') or [])}",
            f"Метки игроков Steam: {', '.join(tags)}" if tags else "",
            f"Возможности: {', '.join(x['description'] for x in d.get('categories') or [])}",
            f"Цена: {(d.get('price_overview') or {}).get('final_formatted') or 'бесплатно или нет данных'}"]
    return StorePage(appid=appid, name=d.get("name") or "", developers=d.get("developers") or [],
                     about=clean(d.get("about_the_game") or d.get("detailed_description"))[:12000],
                     meta="\n".join(m for m in meta if m), release=(d.get("release_date") or {}).get("date"),
                     tags=tags)


def parse_reviews(data: dict, min_words: int) -> list[SteamReview]:
    out = []
    for x in data.get("reviews") or []:
        text = (x.get("review") or "").strip()
        if words(text) < min_words:
            continue
        a = x.get("author") or {}
        ts = x.get("timestamp_created")
        out.append(SteamReview(id=str(x["recommendationid"]), text=text[:4000], voted_up=bool(x.get("voted_up")),
                               hours=round((a.get("playtime_at_review") or a.get("playtime_forever") or 0) / 60, 1),
                               lang=x.get("language"), early_access=bool(x.get("written_during_early_access")),
                               posted_at=datetime.fromtimestamp(ts, UTC) if ts else None))
    return out


def sample(reviews: list[SteamReview], cap: int) -> list[SteamReview]:
    """Up to `cap` reviews, taken in turn from each recommend x hours cell, newest first within a cell."""
    cells: dict[tuple[bool, int], list[SteamReview]] = {}
    for r in reviews:
        cells.setdefault(r.cell, []).append(r)
    queues = [cells[k] for k in sorted(cells)]
    out: list[SteamReview] = []
    while len(out) < cap and any(queues):
        for q in queues:
            if q and len(out) < cap:
                out.append(q.pop(0))
    return out


def parse_news(data: dict) -> list[dict]:
    """The developer's own announcements (feed_type 1): patch notes and devlogs, not press reposts."""
    out = []
    for n in (data.get("appnews") or {}).get("newsitems") or []:
        if n.get("feed_type") != 1:
            continue
        text = clean(n.get("contents"))
        if words(text) >= 40:
            out.append({"gid": str(n.get("gid")), "title": n.get("title") or "", "text": text[:6000],
                        "date": datetime.fromtimestamp(n["date"], UTC) if n.get("date") else None})
    return out


class Steam:
    def __init__(self, rps: float, min_words: int = 15):
        self.web = WebClient("steam", rps)
        self.min_words = min_words

    async def aclose(self) -> None:
        await self.web.aclose()

    async def find(self, title: str) -> list[dict]:
        j = await self.web.get_json(f"{STORE}/api/storesearch/", {"term": title, "cc": "us", "l": "english"})
        return j.get("items") or []

    async def resolve(self, title: str, developer: str | None) -> tuple[int | None, dict]:
        """App id by the exact normalised title; the developer is checked on the store page by the caller.
        Fuzzy matches are not taken: a wrong game poisons every factor of the profile."""
        items = await self.find(title)
        want = norm(title)
        for it in items:
            if norm(it.get("name", "")) == want:
                return int(it["id"]), {"name": it["name"]}
        return None, {"candidates": [it.get("name") for it in items[:5]]}

    async def store(self, appid: int) -> StorePage:
        data = await self.web.get_json(f"{STORE}/api/appdetails", {"appids": appid, "cc": "us", "l": "english"})
        page = await self.web.get_text(f"{STORE}/app/{appid}/", cookies=COOKIES)
        return parse_store(appid, data, page["text"])

    async def reviews(self, appid: int, pages: int, known: set[str]) -> tuple[list[SteamReview], dict]:
        """Newest reviews, page by page, until a known one or `pages` pages; plus the totals Steam reports."""
        out, cursor, summary = [], "*", {}
        for _ in range(pages):
            j = await self.web.get_json(f"{STORE}/appreviews/{appid}", {
                "json": 1, "filter": "recent", "language": "all", "purchase_type": "all", "review_type": "all",
                "num_per_page": 100, "cursor": cursor})
            summary = summary or j.get("query_summary") or {}
            batch = j.get("reviews") or []
            fresh = parse_reviews(j, self.min_words)
            out += [r for r in fresh if r.id not in known]
            if not batch or any(str(x.get("recommendationid")) in known for x in batch):
                break
            if j.get("cursor") in (None, cursor):
                break
            cursor = j["cursor"]
        totals = {k: summary.get(k) for k in ("total_reviews", "total_positive", "review_score_desc")}
        return out, totals

    async def news(self, appid: int) -> list[dict]:
        j = await self.web.get_json(f"{API}/ISteamNews/GetNewsForApp/v2/", {"appid": appid, "count": 15,
                                                                            "maxlength": 0})
        return parse_news(j)
