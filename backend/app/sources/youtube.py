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
SEARCH_RESULTS = 15
MIN_VIDEO_S = 10 * 60
SILENT = re.compile(r"no[\s-]*(?:commentary|talking|voice)|without commentary|silent playthrough|\bnc\b|"
                    r"без\s+комментари|без\s+слов", re.IGNORECASE)
SKIP_WORDS = re.compile(r"\b(trailer|teaser|reveal|soundtrack|ost|review|reaction|shorts|livestream)\b", re.IGNORECASE)
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
    return re.findall(r"[a-z0-9]+", s.lower().replace("'", ""))


def title_matches(game_title: str, video_title: str) -> bool:
    """Every significant word of the game's title is in the video's title ("Marvel's Wolverine" → marvels, wolverine)."""
    stop = {"the", "of", "a", "an", "and", "edition", "remastered", "remake", "deluxe", "complete", "definitive"}
    need = [w for w in _words(game_title) if w not in stop] or _words(game_title)
    have = set(_words(video_title))
    return all(w in have for w in need)


def is_silent(v: Video) -> bool:
    """A playthrough the author says is without commentary: no voice, so no opinions to report."""
    return bool(SILENT.search(f"{v.title}\n{v.description[:500]}"))


def pick_video(game_title: str, videos: list[Video], prefer_s: int, max_s: int) -> Video | None:
    """Most viewed real let's play of this game: right title, not a trailer or a review, long enough to show play.
    A video with the author's commentary wins over a silent one, since opinions are half of what we report; among
    those, videos up to `prefer_s` win, in case the daily YouTube allowance counts whole videos."""
    ok = [v for v in videos if title_matches(game_title, v.title) and not SKIP_WORDS.search(v.title)
          and MIN_VIDEO_S <= v.duration_s <= max_s]
    if not ok:
        return None
    return max(ok, key=lambda v: (not is_silent(v), v.duration_s <= prefer_s, v.views))


def pick_clips(v: Video, n: int, clip_s: int) -> list[Clip]:
    """n clips spread over the video: by chapters when it has them, otherwise at 10%, 45% and 80% of its length.
    The first minutes are skipped either way: intros, menus and logos say little about the game."""
    clip_s = min(clip_s, max(60, v.duration_s // (n + 1)))
    if v.chapters and len(v.chapters) > n:
        body = v.chapters[1:]  # chapter 0 is the intro
        idx = sorted({round(i * (len(body) - 1) / max(1, n - 1)) for i in range(n)})
        starts = [(body[i][0], body[i][1]) for i in idx]
    else:
        fractions = [0.10, 0.45, 0.80] if n == 3 else [(i + 0.5) / n for i in range(n)]
        starts = [(int(v.duration_s * f), None) for f in fractions]
    clips = []
    for s, label in starts:
        s = min(s, max(0, v.duration_s - clip_s))
        if clips and s < clips[-1].end_s:
            continue
        clips.append(Clip(s, min(v.duration_s, s + clip_s), label))
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

    async def search(self, game_title: str) -> list[Video]:
        found = await self._get("/search", {
            "part": "snippet", "type": "video", "maxResults": SEARCH_RESULTS, "order": "relevance",
            "q": f"{game_title} gameplay walkthrough", "videoEmbeddable": "true", "safeSearch": "none"})
        ids = [i["id"]["videoId"] for i in found.get("items", []) if i.get("id", {}).get("videoId")]
        if not ids:
            return []
        details = await self._get("/videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)})
        out = []
        for i in details.get("items", []):
            sn, st, cd = i["snippet"], i.get("statistics", {}), i.get("contentDetails", {})
            if sn.get("liveBroadcastContent", "none") != "none":
                continue  # live and upcoming streams have no fixed length to clip
            dur = parse_duration(cd.get("duration", ""))
            desc = sn.get("description", "")
            out.append(Video(id=i["id"], title=sn.get("title", ""), channel=sn.get("channelTitle", ""),
                             views=int(st.get("viewCount", 0)), duration_s=dur, description=desc,
                             chapters=parse_chapters(desc, dur)))
        return out
