"""Watch when Metacritic's "New Releases" block (top 20 on /game/) changes.

  backend/.venv/bin/python research/nr_watch.py [--every 600] [--hours 48]

Every `--every` seconds it reads the block three ways and logs any change:
  origin  the API query the site uses, with a cache-busting parameter: what Metacritic's backend has right now
  cdn     the exact same query without it: what the site's servers get from Cloudflare
  page    the 20 games in the server-rendered /game/ page: what a visitor sees
Timestamps come from the server's Date header. Output in research/nr_watch/:
  snapshots.jsonl  every poll;  events.jsonl  only changes (entered / left / reordered / new metascore)
Stop it with:  pkill -f research/nr_watch.py
"""

import argparse
import json
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
API = ("https://backend.metacritic.com/finder/metacritic/web?componentName=new-releases-carousel"
       "&componentDisplayName=Newly+Released&componentType=ProductList&sortBy=-releaseDate&metaScoreMin=1"
       "&offset=0&limit=20&mcoTypeId=13")
PAGE = "https://www.metacritic.com/game/"
OUT = Path(__file__).parent / "nr_watch"


def server_time(r: httpx.Response) -> str:
    return parsedate_to_datetime(r.headers["date"]).isoformat()


def read_api(c: httpx.Client, bust: bool) -> dict:
    r = c.get(API + (f"&_cb={time.time_ns()}" if bust else ""))
    items = r.json()["data"]["items"]
    return {"at": server_time(r), "cache": r.headers.get("cf-cache-status"), "age": r.headers.get("age"),
            "last_modified": r.headers.get("last-modified"),
            "games": [{"slug": i["slug"], "released": i["releaseDate"],
                       "metascore": (i.get("criticScoreSummary") or {}).get("score")} for i in items]}


def read_page(c: httpx.Client) -> dict:
    r = c.get(PAGE)
    html = r.text
    start = html.find('aria-label="New Releases content"')
    slugs = list(dict.fromkeys(re.findall(r'href="/game/([a-z0-9-]+)/"', html[start:])))[:20] if start >= 0 else []
    return {"at": server_time(r), "cache": r.headers.get("cf-cache-status"), "age": r.headers.get("age"),
            "last_modified": r.headers.get("last-modified"), "games": [{"slug": s} for s in slugs]}


def diff(prev: dict, cur: dict) -> dict | None:
    a = {g["slug"]: g for g in prev["games"]}
    b = {g["slug"]: g for g in cur["games"]}
    change = {
        "entered": [s for s in b if s not in a],
        "left": [s for s in a if s not in b],
        "new_metascore": [{"slug": s, "from": a[s].get("metascore"), "to": b[s].get("metascore")}
                          for s in b if s in a and a[s].get("metascore") != b[s].get("metascore")],
        "reordered": [g["slug"] for g in prev["games"]] != [g["slug"] for g in cur["games"]],
    }
    return change if change["entered"] or change["left"] or change["new_metascore"] else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=600)
    ap.add_argument("--hours", type=float, default=48)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    c = httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
    last: dict[str, dict] = {}
    deadline = time.time() + a.hours * 3600
    while time.time() < deadline:
        for source, read in (("origin", lambda: read_api(c, True)), ("cdn", lambda: read_api(c, False)),
                             ("page", lambda: read_page(c))):
            try:
                snap = {"source": source, **read()}
            except Exception as e:  # noqa: BLE001  a failed poll is logged and retried next round
                snap = {"source": source, "error": f"{type(e).__name__}: {e}"[:200]}
            with (OUT / "snapshots.jsonl").open("a") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            if snap.get("games"):
                if source in last and (d := diff(last[source], snap)):
                    with (OUT / "events.jsonl").open("a") as f:
                        f.write(json.dumps({"source": source, "at": snap["at"], "seen_since": last[source]["at"],
                                            "age": snap["age"], "last_modified": snap["last_modified"], **d},
                                           ensure_ascii=False) + "\n")
                last[source] = snap
        time.sleep(a.every)


if __name__ == "__main__":
    main()
