"""Steam: find the game, its store page, player reviews and the developer's news. Public endpoints, no key.

Reviews are the source that decides whether a game has enough to profile (docs/game-profile.md): they carry the
player's hours and whether they recommend the game, which is what "for whom it stops working" stands on. They are
sampled across recommend x hours cells, so a hundred reviews cover quitters and veterans, fans and the disappointed.
"""

import html
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.sources.web import WebClient

STORE = "https://store.steampowered.com"
API = "https://api.steampowered.com"
COOKIES = {"birthtime": "568022401", "mature_content": "1", "wants_mature_content": "1"}  # past the age gate
HOUR_BANDS = (2, 10, 50)  # < 2 h: the refund window; 2-10; 10-50; 50+
# Player reviews moved to the Web API in October 2026: store.steampowered.com/appreviews now redirects to the store
# front page. Same fields, parameters as JSON in input_json, enums as numbers (Steamworks docs, IUserReviewsService).
REVIEWS_API = f"{API}/IUserReviewsService/GetAppReviews/v1/"
FILTER_RECENT, PURCHASE_ALL, REVIEW_ALL = 1, 1, 0
CANARY_APP = 730  # Counter-Strike 2: always on sale, always reviewed, always in the news
PAUSE_AFTER = 5  # consecutive failures of one endpoint...
PAUSE_S = 3600  # ...pause it for an hour, then one call decides


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


class SourcePaused(Exception):
    """An endpoint failed PAUSE_AFTER times in a row: skipped until its pause ends, instead of hammering it."""

    def __init__(self, part: str, until: float):
        super().__init__(f"{part} paused for {max(0, round((until - time.monotonic()) / 60))} min after "
                         f"{PAUSE_AFTER} failures in a row")
        self.part = part


class Breaker:
    """Per endpoint: consecutive failures, and a pause once there are too many. One worker, one instance."""

    def __init__(self, on_pause: Callable[[str, str], Awaitable[None]] | None = None):
        self.fails: dict[str, int] = {}
        self.until: dict[str, float] = {}
        self.on_pause = on_pause

    def check(self, part: str) -> None:
        if self.until.get(part, 0) > time.monotonic():
            raise SourcePaused(part, self.until[part])

    async def result(self, part: str, error: Exception | None) -> None:
        if error is None:
            self.fails[part], self.until[part] = 0, 0
            return
        self.fails[part] = self.fails.get(part, 0) + 1
        if self.fails[part] >= PAUSE_AFTER and self.until.get(part, 0) <= time.monotonic():
            self.until[part] = time.monotonic() + PAUSE_S
            if self.on_pause:
                await self.on_pause(part, f"{type(error).__name__}: {error}"[:300])


def reviews_input(appid: int, cursor: str, per_page: int = 100) -> dict:
    return {"appid": appid, "filter": FILTER_RECENT, "languages": ["all"], "review_type": REVIEW_ALL,
            "purchase_type": PURCHASE_ALL, "num_per_page": per_page, "cursor": cursor}


def unwrap(j: dict) -> dict:
    """Web API answers come as {"response": {...}}; tolerate both shapes."""
    return j.get("response", j) if isinstance(j, dict) else {}


class Steam:
    def __init__(self, rps: float, min_words: int = 15,
                 on_pause: Callable[[str, str], Awaitable[None]] | None = None):
        self.web = WebClient("steam", rps)
        self.min_words = min_words
        self.breaker = Breaker(on_pause)

    async def aclose(self) -> None:
        await self.web.aclose()

    async def _guarded(self, part: str, call: Callable[[], Awaitable]):
        self.breaker.check(part)
        try:
            out = await call()
        except Exception as e:
            await self.breaker.result(part, e)
            raise
        await self.breaker.result(part, None)
        return out

    async def find(self, title: str) -> list[dict]:
        j = await self._guarded("steam_store", lambda: self.web.get_json(
            f"{STORE}/api/storesearch/", {"term": title, "cc": "us", "l": "english"}, health_as="steam_store"))
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
        async def call():
            data = await self.web.get_json(f"{STORE}/api/appdetails", {"appids": appid, "cc": "us", "l": "english"},
                                           health_as="steam_store")
            page = await self.web.get_text(f"{STORE}/app/{appid}/", cookies=COOKIES, health_as="steam_store")
            return parse_store(appid, data, page["text"])

        return await self._guarded("steam_store", call)

    async def reviews(self, appid: int, pages: int, known: set[str]) -> tuple[list[SteamReview], dict]:
        """Newest reviews, page by page, until a known one or `pages` pages; plus the totals Steam reports."""
        out, cursor, summary = [], "*", {}
        for _ in range(pages):
            params = {"input_json": json.dumps(reviews_input(appid, cursor), separators=(",", ":"))}
            j = unwrap(await self._guarded("steam_reviews", lambda p=params: self.web.get_json(
                REVIEWS_API, p, health_as="steam_reviews")))
            if "reviews" not in j and "query_summary" not in j:
                raise ValueError(f"GetAppReviews: unexpected answer, keys {sorted(j)[:6]}")
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
        j = await self._guarded("steam_news", lambda: self.web.get_json(
            f"{API}/ISteamNews/GetNewsForApp/v2/", {"appid": appid, "count": 15, "maxlength": 0},
            health_as="steam_news"))
        return parse_news(j)

    # ---------- canaries: one small request each, with a check of the answer's shape ----------

    async def canary_reviews(self) -> str:
        params = {"input_json": json.dumps(reviews_input(CANARY_APP, "*", per_page=1), separators=(",", ":"))}
        j = unwrap(await self.web.get_json(REVIEWS_API, params, health_as="steam_reviews"))
        total = (j.get("query_summary") or {}).get("total_reviews")
        if not j.get("reviews") or not total:
            raise ValueError(f"GetAppReviews {CANARY_APP}: no reviews in the answer")
        return f"CS2: {total} отзывов, ответ в норме"

    async def canary_store(self) -> str:
        j = await self.web.get_json(f"{STORE}/api/appdetails", {"appids": CANARY_APP, "cc": "us", "l": "english"},
                                    health_as="steam_store")
        d = (j.get(str(CANARY_APP)) or {})
        if not d.get("success") or not (d.get("data") or {}).get("name"):
            raise ValueError(f"appdetails {CANARY_APP}: success={d.get('success')}")
        return f"appdetails: {d['data']['name']}"

    async def canary_news(self) -> str:
        j = await self.web.get_json(f"{API}/ISteamNews/GetNewsForApp/v2/", {"appid": CANARY_APP, "count": 1,
                                                                            "maxlength": 1}, health_as="steam_news")
        items = (j.get("appnews") or {}).get("newsitems")
        if items is None:
            raise ValueError("GetNewsForApp: no appnews.newsitems")
        return f"новостей в ответе: {len(items)}"
