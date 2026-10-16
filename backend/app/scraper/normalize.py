"""Pure functions: Metacritic JSON -> our dataclasses. No I/O, covered by fixture tests."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date


@dataclass
class PlatformInfo:
    name: str
    slug: str
    release_date: date | None
    metascore: int | None
    critic_count: int | None


@dataclass
class GameCard:
    id: int
    slug: str
    title: str
    description: str | None
    release_date: date | None
    genres: list[str]
    developer: str | None
    publisher: str | None
    rating: str | None
    must_play: bool
    cover_url: str | None
    video_title: str | None
    video_embed_url: str | None
    video_manifest_url: str | None
    metascore: int | None
    critic_count: int
    userscore: float | None
    user_count: int
    platforms: list[PlatformInfo] = field(default_factory=list)


@dataclass
class FinderItem:
    id: int
    slug: str
    title: str
    release_date: date | None
    metascore: int | None
    critic_count: int
    platforms: list[str]


@dataclass
class ReviewData:
    id: str
    kind: str
    platform: str | None
    author: str | None
    publication: str | None
    score: float | None
    date: date | None
    quote: str | None
    url: str | None
    spoiler: bool
    word_count: int


def _date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _companies(item: dict, type_name: str) -> str | None:
    companies = (item.get("production") or {}).get("companies") or []
    names = list(dict.fromkeys(c["name"] for c in companies if c.get("typeName") == type_name and c.get("name")))
    return ", ".join(names) or None


def _cover(item: dict, image_base: str) -> str | None:
    images = [i for i in item.get("images") or [] if i.get("bucketPath")]
    main = next((i for i in images if i.get("typeName") == "mainImage"), images[0] if images else None)
    return f"{image_base}{main['bucketPath']}" if main else None


def parse_composer(data: dict, image_base: str) -> GameCard:
    comps = {c["meta"]["componentName"]: c.get("data") or {} for c in data["components"]}
    item = comps["product"]["item"]
    critic = (comps.get("critic-score-summary") or {}).get("item") or {}
    user = (comps.get("user-score-summary") or {}).get("item") or {}
    video = item.get("video") or {}
    return GameCard(
        id=int(item["id"]),
        slug=item["slug"],
        title=item["title"],
        description=(item.get("description") or "").strip() or None,
        release_date=_date(item.get("releaseDate")),
        genres=[g["name"] for g in item.get("genres") or [] if g.get("name")],
        developer=_companies(item, "Developer"),
        publisher=_companies(item, "Publisher"),
        rating=item.get("rating"),
        must_play=bool(item.get("mustPlay")),
        cover_url=_cover(item, image_base),
        video_title=video.get("title"),
        video_embed_url=video.get("embedUrl"),
        video_manifest_url=video.get("manifestUrl"),
        # Metacritic sends score 0 when nobody has rated yet; the site shows "tbd" for that, so do we.
        metascore=critic.get("score") if critic.get("reviewCount") else None,
        critic_count=critic.get("reviewCount") or 0,
        userscore=user.get("score") if user.get("reviewCount") else None,
        user_count=user.get("reviewCount") or 0,
        platforms=[
            PlatformInfo(
                name=p["name"],
                slug=p["slug"],
                release_date=_date(p.get("releaseDate")),
                metascore=(p.get("criticScoreSummary") or {}).get("score"),
                critic_count=(p.get("criticScoreSummary") or {}).get("reviewCount"),
            )
            for p in item.get("platforms") or []
            if p.get("slug")
        ],
    )


def parse_finder_item(i: dict) -> FinderItem:
    css = i.get("criticScoreSummary") or {}
    return FinderItem(
        id=int(i["id"]),
        slug=i["slug"],
        title=i["title"],
        release_date=_date(i.get("releaseDate")),
        metascore=css.get("score"),
        critic_count=css.get("reviewCount") or 0,
        platforms=[p["name"] for p in i.get("platforms") or [] if p.get("name")],
    )


def critic_review_id(r: dict) -> str:
    """Critic reviews carry no id; this key is stable across re-fetches of the same review."""
    parts = [
        r.get("publicationSlug") or r.get("publicationName") or "",
        r.get("authorSlug") or r.get("author") or "",
        r.get("date") or "",
        str(r.get("score")),
        r.get("platform") or "",
        (r.get("quote") or "")[:80],
    ]
    return "c:" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:32]


def user_review_id(r: dict) -> str:
    return f"u:{r['id']}"


def parse_review(r: dict, kind: str) -> ReviewData:
    critic = kind == "critic"
    return ReviewData(
        id=critic_review_id(r) if critic else user_review_id(r),
        kind=kind,
        platform=r.get("platform") if isinstance(r.get("platform"), str) else None,
        author=r.get("author") or None,
        publication=r.get("publicationName") if critic else None,
        score=r.get("score"),
        date=_date(r.get("date")),
        quote=(r.get("quote") or "").strip() or None,
        url=r.get("url") or None,
        spoiler=bool(r.get("spoiler")),
        word_count=len(re.findall(r"\w+", r.get("quote") or "")),
    )
