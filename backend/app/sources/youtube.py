"""Finding a let's play through the YouTube Data API v3 and choosing which minutes of it the model watches.

Official API only: search.list (100 units) and videos.list (1 unit) out of a free 10 000 a day. The video itself is
never downloaded; Gemini reads the public YouTube URL. The key goes in a header, so it never appears in a URL, a log
line or an error message.
"""

import re
from dataclasses import dataclass, field

import httpx

from app.core import faults, tape

API = "https://www.googleapis.com/youtube/v3"
RESULTS_PER_QUERY = 10
MIN_VIDEO_S = 10 * 60
SILENT = re.compile(r"no[\s-]*(?:commentary|talking|voice)|without commentary|silent playthrough|\bnc\b|"
                    r"без\s+комментари|без\s+слов", re.IGNORECASE)
SKIP_WORDS = re.compile(r"\b(trailer|teaser|reveal|soundtrack|ost|review|reaction|shorts|livestream)\b", re.IGNORECASE)
LETS_PLAY = re.compile(r"walkthrough|let'?s\s*play|playthrough|gameplay|\bpart\s*\d|\bep(?:isode)?\.?\s*\d|full\s*game|"
                       r"\bblind\b|прохождени|летсплей|часть\s*\d", re.IGNORECASE)
# Streams talk to the chat more than about the game: mixed content, of no use to us even when they show play.
STREAM = re.compile(r"\b(live\s*stream|livestream|stream|streaming|streamed|vod|twitch|kick\.com|live)\b|стрим|трансляц|"
                    r"🔴|\[live\]|\(live\)", re.IGNORECASE)
DEMO = re.compile(r"\b(demo|prologue|playtest|beta|alpha)\b|демо", re.IGNORECASE)
STOP = {"the", "of", "a", "an", "and", "edition", "remastered", "remake", "deluxe", "complete", "definitive", "gold",
        "ultimate", "collection", "bundle", "goty", "director", "directors", "cut", "enhanced"}
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_TS = re.compile(r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*[-–—:|]?\s*(.+)$")


class YouTubeError(Exception):
    pass


@dataclass
class Video:
    id: str
    title: str
    channel: str
    views: int
    duration_s: int
    description: str = ""
    chapters: list[tuple[int, str]] = field(default_factory=list)
    was_live: bool = False  # a recorded live broadcast: YouTube keeps liveStreamingDetails on the archive

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass
class Clip:
    start_s: int
    end_s: int
    label: str | None = None

    @property
    def length(self) -> int:
        return self.end_s - self.start_s


def parse_duration(iso: str) -> int:
    """ISO 8601 durations as videos.list returns them: PT1H2M3S, PT45M, P1DT2H."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def to_seconds(ts: str) -> int:
    n = 0
    for part in ts.split(":"):
        n = n * 60 + int(part)
    return n


def parse_chapters(description: str, duration_s: int) -> list[tuple[int, str]]:
    """YouTube's own rule: chapters are timestamp lines that start at 0:00, at least three, in increasing order."""
    out: list[tuple[int, str]] = []
    for line in description.splitlines():
        m = _TS.match(line)
        if m:
            t = to_seconds(m.group(1))
            if t < duration_s and (not out or t > out[-1][0]):
                out.append((t, m.group(2).strip()[:120]))
    return out if len(out) >= 3 and out[0][0] == 0 else []


def _words(s: str) -> list[str]:
    """Words for matching titles: 40,000 and 40K are one number, Roman numerals are numbers, apostrophes go."""
    s = (s or "").lower().replace("'", "").replace("’", "")
    s = re.sub(r"(\d)[,.](\d{3})\b", r"\1\2", s)
    s = re.sub(r"\b(\d+)k\b", r"\g<1>000", s)
    return [ROMAN.get(w, w) for w in re.findall(r"[a-z0-9]+", s)]


def required_words(game_title: str) -> tuple[set[str], set[str]]:
    """(numbers, words) a video title must have. With a subtitle ("Warhammer 40,000: Boltgun 2") the part after the
    colon is the game and the franchise before it is optional: videos say "Boltgun 2", "WH40K Boltgun 2". Without
    one, the whole title counts. Numbers always count: "2" is what tells Boltgun 2 from Boltgun."""
    core = game_title.rsplit(":", 1)[-1] if ":" in game_title else game_title
    words = [w for w in _words(core) if w not in STOP]
    if not any(not w.isdigit() and len(w) >= 3 for w in words):  # "Deluxe Edition" after the colon names nothing
        words = [w for w in _words(game_title) if w not in STOP]
    return {w for w in words if w.isdigit()}, {w for w in words if not w.isdigit()}


def title_matches(game_title: str, video_title: str) -> bool:
    numbers, words = required_words(game_title)
    have = set(_words(video_title))
    if not numbers <= have:
        return False
    # short titles must be there whole; a long one may lose its brand ("Toy Story 3" without "Disney/Pixar")
    need = len(words) if len(words) <= 3 else -(-len(words) // 2)
    return len(words & have) >= max(1, need)


def is_silent(v: Video) -> bool:
    """A playthrough the author says is without commentary: no voice, so no opinions to report."""
    return bool(SILENT.search(f"{v.title}\n{v.description[:500]}"))


def why_not(game_title: str, v: Video, *, min_views: int, max_s: int, released: bool) -> str | None:
    """Why a video is not a let's play of this game worth watching, or None when it is."""
    if not title_matches(game_title, v.title):
        return "другая игра или не то название"
    if v.was_live or STREAM.search(v.title):
        return "запись стрима"
    if SKIP_WORDS.search(v.title):
        return "трейлер или обзор"
    if released and DEMO.search(v.title):
        return "демо, игра уже вышла"
    if v.duration_s < MIN_VIDEO_S:
        return "короче 10 минут"
    if v.duration_s > max_s:
        return "длиннее 8 часов"
    if v.views < min_views:
        return f"меньше {min_views} просмотров"
    return None


def pick_video(game_title: str, videos: list[Video], *, min_views: int, max_s: int, released: bool) -> Video | None:
    """The most viewed real let's play of this game: titled like one, with the author's commentary if possible (opinions
    are half of what we report). None rather than a weak video: "no let's plays yet" is the honest answer."""
    ok = [v for v in videos if why_not(game_title, v, min_views=min_views, max_s=max_s, released=released) is None]
    # a let's play by its title first (essays and reviews rarely say "walkthrough" or "part 1"), then a voice, then views
    return max(ok, key=lambda v: (bool(LETS_PLAY.search(v.title)), not is_silent(v), v.views)) if ok else None


def pick_clips(v: Video, total_s: int = 1200, min_s: int = 300, max_s: int = 390) -> list[Clip]:
    """About `total_s` of the video in clips of min_s..max_s, spread over all of it: three of 6.5 min for a video up
    to an hour, four of 5 min for a longer one, by chapters when it has them. The opening minutes are skipped:
    intros, menus and logos say little about the game. A short video gets proportionally less."""
    n = 3 if v.duration_s <= 3600 else 4
    clip_s = max(min_s, min(max_s, total_s // n))
    clip_s = min(clip_s, max(60, v.duration_s // (n + 1)))
    if v.chapters and len(v.chapters) > n:
        body = v.chapters[1:]  # chapter 0 is the intro
        idx = sorted({round(i * (len(body) - 1) / max(1, n - 1)) for i in range(n)})
        starts = [(body[i][0], body[i][1]) for i in idx]
    else:
        fractions = [0.10, 0.45, 0.80] if n == 3 else [0.08, 0.33, 0.58, 0.83]
        starts = [(int(v.duration_s * f), None) for f in fractions]
    clips: list[Clip] = []
    for start, label in starts:
        start = min(start, max(0, v.duration_s - clip_s))
        if clips and start < clips[-1].end_s:
            continue
        clips.append(Clip(start, min(v.duration_s, start + clip_s), label))
    return clips


class YouTube:
    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(base_url=API, timeout=20, headers={"X-goog-api-key": api_key})

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        if kind := faults.pick("youtube"):
            try:
                return self._json_of(faults.http_response(kind, f"{API}{path}"))
            except httpx.HTTPError as e:
                raise YouTubeError(f"YouTube Data API недоступен: {type(e).__name__}") from None
        return await tape.call("youtube", {"path": path, "params": params}, lambda: self._get_live(path, params),
                               summary=f"{path} {params.get('q') or params.get('id', '')}"[:200])

    async def _get_live(self, path: str, params: dict) -> dict:
        try:
            r = await self.http.get(path, params=params)
        except httpx.HTTPError as e:
            raise YouTubeError(f"YouTube Data API недоступен: {type(e).__name__}") from None
        return self._json_of(r)

    @staticmethod
    def _json_of(r: httpx.Response) -> dict:
        if r.status_code != 200:
            reason = ""
            try:
                err = r.json()["error"]
                reason = (err.get("errors") or [{}])[0].get("reason") or err.get("status") or ""
            except Exception:  # noqa: BLE001, S110 - only the reason is wanted, the body may be anything
                pass
            raise YouTubeError(f"YouTube Data API: {r.status_code} {reason}".strip())
        try:
            return r.json()
        except ValueError:
            raise YouTubeError("YouTube Data API: ответ не JSON") from None

    async def search(self, queries: list[str], per_query: int = RESULTS_PER_QUERY) -> list[Video]:
        """Every query (100 units each), then one videos.list for all distinct results (1 unit)."""
        ids: list[str] = []
        for q in queries:
            found = await self._get("/search", {
                "part": "snippet", "type": "video", "maxResults": per_query, "order": "relevance",
                "q": q, "videoEmbeddable": "true", "safeSearch": "none"})
            ids += [i["id"]["videoId"] for i in found.get("items", []) if i.get("id", {}).get("videoId")]
        ids = list(dict.fromkeys(ids))[:50]  # videos.list takes up to 50 ids
        if not ids:
            return []
        details = await self._get("/videos", {"part": "snippet,statistics,contentDetails,liveStreamingDetails",
                                              "id": ",".join(ids)})
        out = []
        for i in details.get("items", []):
            sn, st, cd = i["snippet"], i.get("statistics", {}), i.get("contentDetails", {})
            if sn.get("liveBroadcastContent", "none") != "none":
                continue  # live and upcoming streams have no fixed length to clip
            dur = parse_duration(cd.get("duration", ""))
            desc = sn.get("description", "")
            out.append(Video(id=i["id"], title=sn.get("title", ""), channel=sn.get("channelTitle", ""),
                             views=int(st.get("viewCount", 0)), duration_s=dur, description=desc,
                             chapters=parse_chapters(desc, dur), was_live="liveStreamingDetails" in i))
        return out
