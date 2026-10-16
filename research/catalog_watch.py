"""Does the "SEE ALL" catalog change behind our back? Watch per-release-date totals over the last N days.

  backend/.venv/bin/python research/catalog_watch.py [--every 3600] [--days 60] [--hours 72]

Every poll asks the finder, oldest-first (the only order whose total respects both date bounds), how many games
each release date holds, from today+1 (UTC) back to today-N. For dates with at most 50 games one page is the whole
day, so the ids are logged too and additions/removals are exact; bigger days are compared by total only.
Output in research/catalog_watch/:
  snapshots.jsonl  every poll;  events.jsonl  only changes, with the age of the release date in days
Stop it with:  pkill -f research/catalog_watch.py
"""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

API = "https://backend.metacritic.com/finder/metacritic/web"
UA = "GameAnalytics/0.1 (research crawler)"
OUT = Path(__file__).parent / "catalog_watch"
PAGE = 50


def read_day(c: httpx.Client, day: str) -> dict:
    params = {"productType": "games", "sortBy": "releaseDate", "releaseDateMin": day, "releaseDateMax": day,
              "offset": 0, "limit": PAGE, "_cb": time.time_ns()}  # bust the 10-minute Cloudflare cache
    d = c.get(API, params=params).json()["data"]
    ids = sorted(i["id"] for i in d["items"]) if d["totalResults"] <= PAGE else None
    return {"total": d["totalResults"], "ids": ids}


def poll(c: httpx.Client, days: int) -> dict:
    today = datetime.now(UTC).date()
    snap = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "today": today.isoformat(), "days": {}}
    for k in range(-1, days + 1):
        day = (today - timedelta(days=k)).isoformat()
        snap["days"][day] = read_day(c, day)
        time.sleep(1.0)  # ~1 rps: a few dozen requests an hour on top of the worker
    return snap


def changes(prev: dict, cur: dict) -> list[dict]:
    today = datetime.fromisoformat(cur["today"]).date()
    out = []
    for day, b in cur["days"].items():
        a = prev["days"].get(day)
        if a is None or a["total"] == b["total"] and a["ids"] == b["ids"]:
            continue
        e = {"at": cur["at"], "day": day, "age_days": (today - datetime.fromisoformat(day).date()).days,
             "from": a["total"], "to": b["total"]}
        if a["ids"] is not None and b["ids"] is not None:
            e["added"] = sorted(set(b["ids"]) - set(a["ids"]))
            e["removed"] = sorted(set(a["ids"]) - set(b["ids"]))
        out.append(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=3600)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--hours", type=float, default=72)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    stop = time.time() + a.hours * 3600
    prev = None
    with httpx.Client(headers={"User-Agent": UA}, timeout=30) as c:
        while time.time() < stop:
            t0 = time.time()
            try:
                cur = poll(c, a.days)
            except Exception as e:  # noqa: BLE001 - a flaky poll is logged and retried next time
                print(f"{datetime.now(UTC):%H:%M} poll failed: {e}", flush=True)
            else:
                with (OUT / "snapshots.jsonl").open("a") as f:
                    f.write(json.dumps(cur) + "\n")
                if prev:
                    ev = changes(prev, cur)
                    with (OUT / "events.jsonl").open("a") as f:
                        for e in ev:
                            f.write(json.dumps(e) + "\n")
                    print(f"{cur['at']} {len(ev)} day(s) changed: "
                          + ", ".join(f"{e['day']}(age {e['age_days']}) {e['from']}→{e['to']}" for e in ev), flush=True)
                else:
                    print(f"{cur['at']} baseline: {sum(d['total'] for d in cur['days'].values())} games", flush=True)
                prev = cur
            time.sleep(max(0, a.every - (time.time() - t0)))


if __name__ == "__main__":
    main()
