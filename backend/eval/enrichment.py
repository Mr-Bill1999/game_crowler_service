"""How much richer does a game get with each platform: Metacritic first, then Steam, critics' full reviews, YouTube.

  cd backend && .venv/bin/python -m eval.enrichment collect      # texts from every source -> results/enrichment/docs
  .venv/bin/python -m eval.enrichment extract                    # Gemini: evidence per factor, one call per source
  .venv/bin/python -m eval.enrichment score                      # fullness per game and level -> report.json
  .venv/bin/python -m eval.enrichment export                     # flat tables for the report page -> dash/

Input is results/enrichment/games.json, an export of the chosen games from the production database (card, reviews,
raw composer, summaries, deep analyses). Every step caches its output per game and source, so a rerun only pays for
what is missing. Levels are cumulative: L0 Metacritic, then each platform added on top, so the same evidence set
answers "what did this platform add" without a single extra model call.
"""

import argparse
import asyncio
import html
import json
import re
import sys
import time
from pathlib import Path

import httpx
import trafilatura

from app.ai.gemini import GeminiProvider
from app.core.config import get_settings
from app.sources.youtube import title_matches

OUT = Path(__file__).parent / "results" / "enrichment"
DOCS, EVID = OUT / "docs", OUT / "evidence"
UA_BOT = "GameAnalyticsResearch/0.1 (https://github.com/Mr-Bill1999/game_crowler_service) httpx"
UA_WEB = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0 Safari/537.36")
STEAM_COOKIES = {"birthtime": "568022401", "mature_content": "1", "wants_mature_content": "1"}

# Cumulative levels: each adds one platform's sources to everything before it.
LEVELS = [
    ("L0", "Metacritic", ["mc"]),
    ("L1", "+ магазин Steam", ["steam_store"]),
    ("L2", "+ отзывы Steam", ["steam_reviews"]),
    ("L3", "+ новости Steam", ["steam_news"]),
    ("L4", "+ полные рецензии", ["critic_full"]),
    ("L5", "+ YouTube", ["youtube"]),
]
SOURCES = [s for _, _, group in LEVELS for s in group]
FACTORS = {
    1: "ключевая идентичность: роль игрока и основной опыт; что обязано сохраниться, чтобы игра оставалась собой",
    2: "художественный крючок: визуальный язык, звук, повествование, необычное сочетание, делающее игру узнаваемой",
    3: "геймдизайнерское ядро: что игрок делает постоянно, какие решения принимает, как устроены риск и награда",
    4: "эмоциональное предложение: какие переживания создаёт игра и за счёт каких решений",
    5: "противоречия и ограничения: где сильная сторона отталкивает, для кого опыт перестаёт работать",
    6: "рыночная дифференциация: чем игра отличается от ближайших альтернатив и кому это отличие важно",
}


def clean(s: str | None) -> str:
    """HTML or Steam BBCode to plain text."""
    s = re.sub(r"<br\s*/?>|</p>|</li>|\[/?(?:p|list|\*|h\d)\]", "\n", s or "", flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>|\[/?[a-z0-9]+(?:=[^\]]*)?\]", " ", s, flags=re.IGNORECASE)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", "and")).strip()


def words(s: str) -> int:
    return len(re.findall(r"\w+", s))


class Http:
    """One client per host with its own pace: every platform is called at most once a second."""

    def __init__(self):
        self.c = httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.last: dict[str, float] = {}

    async def get(self, url: str, *, params=None, headers=None, cookies=None, pace=1.0) -> httpx.Response:
        host = httpx.URL(url).host
        wait = self.last.get(host, 0) + pace - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last[host] = time.monotonic()
        return await self.c.get(url, params=params, headers=headers or {"User-Agent": UA_BOT}, cookies=cookies)


# ---------- sources ----------

def mc_docs(g: dict) -> list[dict]:
    game, docs = g["game"], []
    if game.get("description"):
        docs.append({"kind": "store_text", "text": game["description"]})
    for r in g["reviews"]:
        if r.get("quote"):
            docs.append({"kind": f"{r['kind']}_review", "text": r["quote"],
                         "meta": {"score": r["score"], "publication": r.get("publication")}})
    stats = next((c.get("data") for c in (g.get("composer") or {}).get("components", [])
                  if c["meta"]["componentName"] == "game-stats"), None) or {}
    item = stats.get("item") or {}
    parts = [f"{k}: " + ", ".join(f"{x['label']} {x['total']}" for x in item.get(k) or [] if x.get("total"))
             for k in ("play_time", "difficulty", "play_status")]
    parts = [p for p in parts if not p.endswith(": ")]
    if parts:
        docs.append({"kind": "gamefaqs_stats", "text": "Опрос игроков GameFAQs. " + "; ".join(parts)})
    return docs


async def wikidata_links(http: Http, slugs: list[str]) -> dict[str, dict]:
    values = " ".join(f'"game/{s}"' for s in slugs)
    q = f"""SELECT ?mc ?item ?steam ?igdb WHERE {{ VALUES ?mc {{ {values} }} ?item wdt:P1712 ?mc.
            OPTIONAL {{ ?item wdt:P1733 ?steam }} OPTIONAL {{ ?item wdt:P5794 ?igdb }} }}"""
    r = await http.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"})
    r.raise_for_status()
    out = {}
    for b in r.json()["results"]["bindings"]:
        out[b["mc"]["value"].removeprefix("game/")] = {
            "wikidata": b["item"]["value"].rsplit("/", 1)[-1],
            "steam": b.get("steam", {}).get("value"), "igdb": b.get("igdb", {}).get("value")}
    return out


async def steam_resolve(http: Http, game: dict, wd: dict | None) -> dict:
    """Steam app id: Wikidata first, else the store search by exact normalised title, checked against the developer."""
    if wd and wd.get("steam"):
        return {"appid": int(wd["steam"]), "method": "wikidata"}
    r = await http.get("https://store.steampowered.com/api/storesearch/",
                       params={"term": game["title"], "cc": "us", "l": "english"})
    items = r.json().get("items") or []
    want = norm(game["title"])
    for it in items:
        if norm(it["name"]) == want:
            return {"appid": it["id"], "method": "search_exact", "name": it["name"]}
    return {"appid": None, "method": "not_found", "candidates": [it["name"] for it in items[:5]]}


async def steam_store(http: Http, appid: int, game: dict) -> tuple[list[dict], dict]:
    r = await http.get("https://store.steampowered.com/api/appdetails",
                       params={"appids": appid, "cc": "us", "l": "english"}, pace=1.5)
    d = (r.json().get(str(appid)) or {}).get("data") or {}
    devs = [norm(x) for x in d.get("developers") or []]
    mc_devs = [norm(x) for x in (game.get("developer") or "").split(",")]
    dev_match = any(a and b and (a in b or b in a) for a in devs for b in mc_devs)
    page = await http.get(f"https://store.steampowered.com/app/{appid}/", headers={"User-Agent": UA_WEB},
                          cookies=STEAM_COOKIES, pace=1.5)
    tags = [clean(t) for t in re.findall(r'class="app_tag"[^>]*>\s*([^<]+?)\s*<', page.text)]
    docs = []
    about = clean(d.get("about_the_game") or d.get("detailed_description"))
    if about:
        docs.append({"kind": "store_text", "text": about[:12000]})
    meta = [f"Жанры Steam: {', '.join(x['description'] for x in d.get('genres') or [])}",
            f"Метки игроков Steam: {', '.join(tags)}" if tags else "",
            f"Возможности: {', '.join(x['description'] for x in d.get('categories') or [])}",
            f"Цена: {(d.get('price_overview') or {}).get('final_formatted', 'бесплатно или нет данных')}"]
    docs.append({"kind": "store_meta", "text": "\n".join(m for m in meta if m)})
    info = {"name": d.get("name"), "developers": d.get("developers"), "dev_match": dev_match,
            "release": (d.get("release_date") or {}).get("date"), "tags": tags,
            "recommendations": (d.get("recommendations") or {}).get("total")}
    return docs, info


async def steam_reviews(http: Http, appid: int, cap: int = 300) -> tuple[list[dict], dict]:
    docs, cursor, summary = [], "*", {}
    while len(docs) < cap:
        r = await http.get(f"https://store.steampowered.com/appreviews/{appid}",
                           params={"json": 1, "filter": "recent", "language": "all", "purchase_type": "all",
                                   "num_per_page": 100, "cursor": cursor, "review_type": "all"})
        j = r.json()
        summary = summary or j.get("query_summary") or {}
        batch = j.get("reviews") or []
        for x in batch:
            text = (x.get("review") or "").strip()
            if words(text) < 15:
                continue
            a = x.get("author") or {}
            docs.append({"kind": "user_review", "text": text[:4000], "meta": {
                "voted_up": x.get("voted_up"), "lang": x.get("language"),
                "hours_at_review": round((a.get("playtime_at_review") or 0) / 60, 1),
                "early_access": x.get("written_during_early_access")}})
        if not batch or j.get("cursor") in (None, cursor):
            break
        cursor = j["cursor"]
    return docs[:cap], {k: summary.get(k) for k in ("total_reviews", "total_positive", "review_score_desc")}


async def steam_news(http: Http, appid: int) -> list[dict]:
    r = await http.get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                       params={"appid": appid, "count": 15, "maxlength": 0})
    items = (r.json().get("appnews") or {}).get("newsitems") or []
    docs = []
    for n in items:
        if n.get("feed_type") != 1:  # 1 = the developer's own announcements; the rest is press reposts
            continue
        text = clean(n.get("contents"))
        if words(text) >= 40:
            docs.append({"kind": "dev_news", "text": f"{n.get('title')}\n{text[:6000]}"})
    return docs


async def critic_full(http: Http, g: dict) -> tuple[list[dict], dict]:
    docs, tried, got = [], 0, 0
    for r in g["reviews"]:
        if r["kind"] != "critic" or not r.get("url"):
            continue
        tried += 1
        try:
            resp = await http.get(r["url"], headers={"User-Agent": UA_WEB}, pace=1.0)
            text = trafilatura.extract(resp.text, include_comments=False) if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            text = None
        if text and words(text) >= 200:
            got += 1
            docs.append({"kind": "critic_full", "text": " ".join(text.split()[:4000]),
                         "meta": {"score": r["score"], "publication": r.get("publication"), "url": r["url"]}})
    return docs, {"tried": tried, "got": got}


async def youtube(http: Http, g: dict, key: str) -> tuple[list[dict], dict]:
    """Comments under the most-discussed matching video, plus facts of a finished deep analysis if there is one."""
    game, docs, info = g["game"], [], {}
    for d in g.get("deep") or []:
        facts = d.get("facts") or {}
        docs.append({"kind": "video_facts", "text": json.dumps(facts, ensure_ascii=False)[:8000]})
    r = await http.get("https://www.googleapis.com/youtube/v3/search", headers={"X-Goog-Api-Key": key},
                       params={"part": "snippet", "q": f"{game['title']} game", "type": "video", "maxResults": 10})
    ids = [it["id"]["videoId"] for it in r.json().get("items") or []
           if it["id"].get("videoId") and title_matches(game["title"], html.unescape(it["snippet"]["title"]))]
    info["matching_videos"] = len(ids)
    if not ids:
        return docs, info
    r = await http.get("https://www.googleapis.com/youtube/v3/videos", headers={"X-Goog-Api-Key": key},
                       params={"part": "snippet,statistics", "id": ",".join(ids)})
    vids = sorted(r.json().get("items") or [], key=lambda v: -int(v["statistics"].get("commentCount", 0)))
    if not vids or not int(vids[0]["statistics"].get("commentCount", 0)):
        return docs, info
    v = vids[0]
    info.update(video=v["id"], title=v["snippet"]["title"], comments=int(v["statistics"]["commentCount"]))
    r = await http.get("https://www.googleapis.com/youtube/v3/commentThreads", headers={"X-Goog-Api-Key": key},
                       params={"part": "snippet", "videoId": v["id"], "maxResults": 100, "order": "relevance",
                               "textFormat": "plainText"})
    texts = [c["snippet"]["topLevelComment"]["snippet"]["textOriginal"] for c in r.json().get("items") or []]
    texts = [t for t in texts if words(t) >= 8]
    if texts:
        docs.append({"kind": "comments", "text": "\n---\n".join(t[:600] for t in texts),
                     "meta": {"video_title": v["snippet"]["title"], "count": len(texts)}})
    return docs, info


async def collect(only: set[str] | None) -> None:
    games = json.loads((OUT / "games.json").read_text())
    DOCS.mkdir(parents=True, exist_ok=True)
    http, key = Http(), get_settings().youtube_api_key
    wd = await wikidata_links(http, [g["game"]["slug"] for g in games])
    for g in games:
        if not only or g["game"]["slug"] in only:
            await collect_game(http, key, g, wd.get(g["game"]["slug"]))


async def collect_game(http: Http, key: str, g: dict, wd: dict | None) -> None:
    game, slug = g["game"], g["game"]["slug"]
    path = DOCS / f"{slug}.json"
    rec = json.loads(path.read_text()) if path.exists() else {"slug": slug, "sources": {}, "info": {}}
    rec["info"]["wikidata"] = wd

    async def step(name: str, call) -> None:
        """One platform: its documents, and what it says about itself in info[name]."""
        if name in rec["sources"]:
            return
        try:
            docs, info = await call()
        except Exception as e:  # noqa: BLE001  one platform failing must not stop the others
            rec["info"][f"{name}_error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"  {slug}: {name} failed: {e}", file=sys.stderr)
            return
        rec["sources"][name] = docs
        if info:
            rec["info"].setdefault(name, {}).update(info)

    async def plain(coro):
        return await coro, None

    await step("mc", lambda: plain(asyncio.sleep(0, mc_docs(g))))
    if "steam" not in rec["info"]:
        rec["info"]["steam"] = await steam_resolve(http, game, wd)
    appid = rec["info"]["steam"].get("appid")
    if appid:
        docs_info = await steam_store(http, appid, game) if "steam_store" not in rec["sources"] else None
        if docs_info:
            rec["sources"]["steam_store"], info = docs_info
            rec["info"]["steam"].update(info)
        await step("steam_reviews", lambda: steam_reviews(http, appid))
        await step("steam_news", lambda: plain(steam_news(http, appid)))
    else:
        for s in ("steam_store", "steam_reviews", "steam_news"):
            rec["sources"].setdefault(s, [])
    await step("critic_full", lambda: critic_full(http, g))
    await step("youtube", lambda: youtube(http, g, key))
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    n = {s: len(rec["sources"].get(s, [])) for s in SOURCES}
    print(f"{slug:48} steam={rec['info']['steam'].get('method'):12} {n}")


# ---------- evidence ----------

SYSTEM = """Ты аналитик игровой индустрии. Тебе дают тексты об одной игре из одного источника. Извлеки из них
доказательства по шести факторам профиля игры:
""" + "\n".join(f"{k}. {v}" for k, v in FACTORS.items()) + """

Правила:
- Только то, что прямо следует из текстов. Ничего не додумывай из знаний об игре или жанре.
- Каждое доказательство — одно короткое утверждение на русском (до 25 слов) и номера документов, на которых оно стоит.
- specific: true, только если утверждение называет конкретную механику, решение, приём или деталь этой игры.
  Общие слова («атмосферная», «затягивает», «красивая графика», «интересный геймплей») — specific: false.
- polarity: plus — сильная сторона, minus — слабость или отторжение, neutral — описание.
- who: для какого игрока это верно или не верно, если тексты это говорят («новички», «фанаты жанра», «наигравшие
  50+ часов»), иначе пусто.
- Для фактора 6 нужны сравнения с другими играми или явное отличие от жанра.
- Маркетинговый текст магазина — заявление разработчика, а не подтверждение: помечай source_is_claim: true.
- Если по фактору в текстах ничего нет, ничего не пиши. Лучше пусто, чем общие слова."""

SCHEMA = {"type": "object", "properties": {"evidence": {"type": "array", "items": {"type": "object", "properties": {
    "factor": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6]},
    "claim": {"type": "string"},
    "docs": {"type": "array", "items": {"type": "integer"}},
    "specific": {"type": "boolean"},
    "polarity": {"type": "string", "enum": ["plus", "minus", "neutral"]},
    "who": {"type": "string"},
    "source_is_claim": {"type": "boolean"},
}, "required": ["factor", "claim", "docs", "specific", "polarity", "source_is_claim"]}}}, "required": ["evidence"]}

BATCH_CHARS = 60_000  # ~15k tokens of documents per call


def doc_line(i: int, d: dict) -> str:
    m = d.get("meta") or {}
    tags = []
    if "voted_up" in m:
        tags.append(("рекомендует" if m["voted_up"] else "не рекомендует") + f", {m['hours_at_review']} ч в игре")
    if m.get("score") is not None:
        tags.append(f"оценка {m['score']}")
    if m.get("publication"):
        tags.append(m["publication"])
    head = f"[{i}] {d['kind']}" + (f" ({'; '.join(tags)})" if tags else "")
    return f"{head}\n{d['text']}"


async def extract(model: str, budget: int) -> None:
    s = get_settings()
    provider = GeminiProvider(s.gemini_keys, default_model=model, rpm=10, concurrency=2)
    EVID.mkdir(parents=True, exist_ok=True)
    spent = sum(json.loads(p.read_text()).get("tokens_in", 0) for p in EVID.glob("*/*.json"))
    for path in sorted(DOCS.glob("*.json")):
        rec = json.loads(path.read_text())
        title = next(g["game"]["title"] for g in json.loads((OUT / "games.json").read_text())
                     if g["game"]["slug"] == rec["slug"])
        for src in SOURCES:
            out = EVID / rec["slug"] / f"{src}.json"
            docs = rec["sources"].get(src) or []
            if out.exists() or not docs:
                continue
            if spent > budget:
                print(f"budget of {budget} input tokens reached, stopping")
                return
            batches, cur, size = [], [], 0
            for i, d in enumerate(docs):
                line = doc_line(i, d)
                if cur and size + len(line) > BATCH_CHARS:
                    batches.append(cur)
                    cur, size = [], 0
                cur.append(line)
                size += len(line)
            batches.append(cur)
            evidence, tin, tout = [], 0, 0
            for b in batches:
                prompt = f"Игра: {title}\nИсточник: {src}\n\nДокументы:\n\n" + "\n\n".join(b)
                res = await provider.generate_json(system=SYSTEM, prompt=prompt, schema=SCHEMA,
                                                   thinking_level=s.llm_thinking_level)
                evidence += res.data.get("evidence") or []
                tin, tout = tin + res.tokens_in, tout + res.tokens_out
            spent += tin
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"source": src, "docs": len(docs), "tokens_in": tin, "tokens_out": tout,
                                       "evidence": evidence}, ensure_ascii=False, indent=1))
            print(f"{rec['slug']:48} {src:14} docs={len(docs):4} ev={len(evidence):3} in={tin:6} out={tout:5}")
    print(f"total input tokens: {spent}")


# ---------- fullness ----------

TARGET = 3  # concrete, independent points that make a factor "covered"


def factor_score(ev: list[dict], factor: int) -> dict:
    """0..1 for one factor. Coverage: concrete evidence not resting on the store's own claims, up to TARGET.
    Corroboration: the factor is seen by two kinds of sources (players, critics, developer, video).
    Factor 5 needs a weakness; factor 6 an explicit comparison is enough to count."""
    mine = [e for e in ev if e["factor"] == factor and e["specific"]]
    independent = [e for e in mine if not e.get("source_is_claim")]
    claims = [e for e in mine if e.get("source_is_claim")]
    # the developer's own words count, but at a third of the weight: they say what the game wants to be
    points = len(independent) + len(claims) / 3
    if factor == 5:
        points = len([e for e in independent if e["polarity"] == "minus"]) + \
            0.5 * len([e for e in independent if e["polarity"] != "minus" and e.get("who")])
    coverage = min(1.0, points / TARGET)
    kinds = {e["_kind"] for e in independent}
    corroboration = min(1.0, len(kinds) / 2)
    return {"score": round(0.75 * coverage + 0.25 * corroboration, 3), "points": round(points, 2),
            "kinds": sorted(kinds)}


KIND_GROUP = {"mc": "critics_players", "steam_reviews": "players", "critic_full": "critics", "steam_store": "developer",
              "steam_news": "developer", "youtube": "video_audience"}


def score() -> None:
    games = {g["game"]["slug"]: g for g in json.loads((OUT / "games.json").read_text())}
    report = []
    for path in sorted(DOCS.glob("*.json")):
        rec = json.loads(path.read_text())
        slug, g = rec["slug"], games[rec["slug"]]
        ev_by_src, llm = {}, {}
        for src in SOURCES:
            p = EVID / slug / f"{src}.json"
            j = json.loads(p.read_text()) if p.exists() else {}
            items, llm[src] = j.get("evidence") or [], (j.get("tokens_in", 0), j.get("tokens_out", 0))
            for e in items:
                e["_kind"] = KIND_GROUP[src]
                e["_src"] = src
            ev_by_src[src] = items
        levels, have = [], []
        for code, label, group in LEVELS:
            have += group
            ev = [e for s in have for e in ev_by_src[s]]
            chars = sum(len(d["text"]) for s in have for d in rec["sources"].get(s) or [])
            factors = {f: factor_score(ev, f) for f in FACTORS}
            fullness = round(100 * sum(x["score"] for x in factors.values()) / len(FACTORS))
            levels.append({"level": code, "label": label, "fullness": fullness, "tokens": round(chars / 3.95),
                           "llm_in": sum(llm[s][0] for s in have), "llm_out": sum(llm[s][1] for s in have),
                           "evidence": len(ev), "specific": sum(e["specific"] for e in ev),
                           "factors": {f: x["score"] for f, x in factors.items()}})
        # Saturation: the full stack, but Steam reviews cut to the first N (newest first, as collected)
        saturation = {}
        n_reviews = len(rec["sources"].get("steam_reviews") or [])
        for n in (0, 25, 50, 100, 200, 300):
            ev = [e for s in SOURCES for e in ev_by_src[s]
                  if s != "steam_reviews" or (e.get("docs") and min(e["docs"]) < n)]
            saturation[n] = round(100 * sum(factor_score(ev, f)["score"] for f in FACTORS) / len(FACTORS))
            if n >= n_reviews:
                break
        report.append({"slug": slug, "title": g["game"]["title"], "critic_count": g["game"]["critic_count"],
                       "saturation": saturation,
                       "user_count": g["game"]["user_count"],
                       "platforms": sorted({p["name"] for p in g.get("platforms") or []}),
                       "steam": rec["info"].get("steam"), "steam_reviews": rec["info"].get("steam_reviews"),
                       "wikidata": rec["info"].get("wikidata"), "critic_full": rec["info"].get("critic_full"),
                       "youtube": rec["info"].get("youtube"),
                       "docs": {s: len(rec["sources"].get(s) or []) for s in SOURCES}, "levels": levels})
    for r in report:
        r["group"] = group_of(r)
    summary = summarize(report)
    (OUT / "report.json").write_text(json.dumps({"games": report, "summary": summary}, ensure_ascii=False, indent=1))
    for row in summary["levels"]:
        print(f"{row['level']} {row['label']:22} median={row['median']:>3} mean={row['mean']:>5} "
              f">=60: {row['enough']:>2}/{len(report)}  llm_in={row['llm_in']}")
    print(f"{'game':44} " + " ".join(f"{c:>4}" for c, _, _ in LEVELS))
    for r in sorted(report, key=lambda r: -r["levels"][-1]["fullness"]):
        print(f"{r['slug'][:44]:44} " + " ".join(f"{lv['fullness']:>4}" for lv in r["levels"]))


ENOUGH = 60  # fullness at which a profile is worth building: most factors have concrete, corroborated evidence
GROUPS = ["без отзывов", "только игроки", "1–3 критика", "4–10 критиков"]


def group_of(r: dict) -> str:
    c, u = r["critic_count"], r["user_count"]
    if c == 0:
        return "без отзывов" if u == 0 else "только игроки"
    return "1–3 критика" if c <= 3 else "4–10 критиков"


def median(xs: list[float]) -> float:
    s = sorted(xs)
    return (s[len(s) // 2] + s[(len(s) - 1) // 2]) / 2 if s else 0


def summarize(report: list[dict]) -> dict:
    levels = []
    for i, (code, label, _) in enumerate(LEVELS):
        vals = [r["levels"][i]["fullness"] for r in report]
        levels.append({
            "level": code, "label": label, "median": median(vals), "mean": round(sum(vals) / len(vals), 1),
            "enough": sum(v >= ENOUGH for v in vals),
            "llm_in": sum(r["levels"][i]["llm_in"] for r in report),
            "llm_out": sum(r["levels"][i]["llm_out"] for r in report),
            "by_group": {grp: median([r["levels"][i]["fullness"] for r in report if r["group"] == grp])
                         for grp in GROUPS},
            "factors": {f: median([r["levels"][i]["factors"][f] for r in report]) for f in FACTORS},
        })
    gains = {}  # what each platform added, per game, over the level before it
    for i in range(1, len(LEVELS)):
        gains[LEVELS[i][0]] = [r["levels"][i]["fullness"] - r["levels"][i - 1]["fullness"] for r in report]
    first_enough = {}
    for r in report:
        hit = next((lv["level"] for lv in r["levels"] if lv["fullness"] >= ENOUGH), None)
        first_enough[hit or "никогда"] = first_enough.get(hit or "никогда", 0) + 1
    return {"levels": levels, "gains": gains, "first_enough": first_enough,
            "groups": {grp: sum(r["group"] == grp for r in report) for grp in GROUPS},
            "steam_linked": sum(bool((r["steam"] or {}).get("appid")) for r in report),
            "wikidata_linked": sum(bool(r["wikidata"]) for r in report)}


SOURCE_LABELS = {"mc": "Metacritic", "steam_store": "Магазин Steam", "steam_reviews": "Отзывы Steam",
                 "steam_news": "Новости Steam", "critic_full": "Полные рецензии", "youtube": "YouTube"}


def export() -> None:
    """Flat tables for the report page: levels, games, Steam saturation, sources. Strict = TARGET 8."""
    rep = json.loads((OUT / "report.json").read_text())
    games = rep["games"]
    slugs = [g["slug"] for g in games]
    evs = {}
    for slug in slugs:
        evs[slug] = {}
        for src in SOURCES:
            p = EVID / slug / f"{src}.json"
            items = json.loads(p.read_text())["evidence"] if p.exists() else []
            for e in items:
                e["_kind"] = KIND_GROUP[src]
            evs[slug][src] = items

    def full(slug, srcs, cut=None):
        ev = [e for s in srcs for e in evs[slug][s]
              if cut is None or s != "steam_reviews" or (e["docs"] and min(e["docs"]) < cut)]
        return round(100 * sum(factor_score(ev, f)["score"] for f in FACTORS) / len(FACTORS))

    def strict(fn):
        global TARGET
        TARGET = 8
        try:
            return fn()
        finally:
            TARGET = 3

    levels = []
    for i, row in enumerate(rep["summary"]["levels"]):
        srcs = [s for _, _, grp in LEVELS[:i + 1] for s in grp]
        st = strict(lambda srcs=srcs: [full(s, srcs) for s in slugs])
        levels.append({"level": row["level"], "label": row["label"], "median": row["median"],
                       "enough": row["enough"], "median_strict": median(st), "enough_strict": sum(v >= ENOUGH for v in st),
                       "llm_in": row["llm_in"], "llm_out": row["llm_out"],
                       **{f"g{k}": v for k, v in enumerate(row["by_group"].values())},
                       **{f"f{f}": round(100 * v) for f, v in row["factors"].items()}})
    rows = []
    for g in games:
        lv = g["levels"]
        first = next((x["level"] for x in lv if x["fullness"] >= ENOUGH), "нет")
        rows.append({"slug": g["slug"], "title": g["title"], "group": g["group"],
                     "steam_reviews": (g["steam_reviews"] or {}).get("total_reviews"),
                     **{x["level"]: x["fullness"] for x in lv},
                     "first_enough": first, "text_tokens_mc": lv[0]["tokens"], "text_tokens_all": lv[-1]["tokens"],
                     "llm_in": lv[-1]["llm_in"], "llm_out": lv[-1]["llm_out"]})
    saturation = []
    for n in (0, 25, 50, 100, 300):
        t3 = [full(s, SOURCES, n) for s in slugs]
        t8 = strict(lambda n=n: [full(s, SOURCES, n) for s in slugs])
        tokens = sum(len(d["text"]) for s in slugs
                     for d in (json.loads((DOCS / f"{s}.json").read_text())["sources"].get("steam_reviews") or [])[:n])
        saturation.append({"reviews": n, "median": median(t3), "enough": sum(v >= ENOUGH for v in t3),
                           "median_strict": median(t8), "enough_strict": sum(v >= ENOUGH for v in t8),
                           "text_tokens": round(tokens / 3.95)})
    sources = []
    for src in SOURCES:
        recs = [json.loads((DOCS / f"{s}.json").read_text()) for s in slugs]
        loss = [full(s, SOURCES) - full(s, [x for x in SOURCES if x != src]) for s in slugs]
        loss8 = strict(lambda src=src: [full(s, SOURCES) - full(s, [x for x in SOURCES if x != src]) for s in slugs])
        sources.append({
            "source": SOURCE_LABELS[src], "games_with_text": sum(bool(r["sources"].get(src)) for r in recs),
            "docs": sum(len(r["sources"].get(src) or []) for r in recs),
            "text_tokens": round(sum(len(d["text"]) for r in recs for d in r["sources"].get(src) or []) / 3.95),
            "llm_in": sum(json.loads((EVID / s / f"{src}.json").read_text())["tokens_in"]
                          for s in slugs if (EVID / s / f"{src}.json").exists()),
            "loss_mean": round(sum(loss) / len(loss), 1), "loss_mean_strict": round(sum(loss8) / len(loss8), 1),
            "alone_median": median([full(s, [src]) for s in slugs])})
    dash = OUT / "dash"
    dash.mkdir(exist_ok=True)
    for name, data in (("levels", levels), ("games", rows), ("saturation", saturation), ("sources", sources)):
        (dash / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"wrote {dash}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["collect", "extract", "score", "export"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--budget", type=int, default=500_000, help="input tokens for the whole extract step")
    a = ap.parse_args()
    if a.step == "collect":
        asyncio.run(collect(set(a.only) if a.only else None))
    elif a.step == "extract":
        asyncio.run(extract(a.model, a.budget))
    elif a.step == "score":
        score()
    else:
        export()


if __name__ == "__main__":
    main()
