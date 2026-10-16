"""Break the stand on purpose, scenario by scenario, and check that the service survives the way it should.

  python3 stand/chaos.py                 every scenario
  python3 stand/chaos.py top_down gemini_daily
  python3 stand/chaos.py --list

Each scenario: reset the stand to its snapshot, inject faults (backend/app/core/faults.py), run one hourly cycle from
tapes, wait for the analyses and facets it queues, then check the database. Exit code 1 if any check fails.
Replay mode throughout: no scenario reaches Metacritic, Gemini or YouTube.
"""

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

STAND = Path(__file__).parent
SH = str(STAND / "stand.sh")


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(args, check=False, cwd=STAND, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)} failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout + r.stderr


def env() -> dict:
    seed = STAND / "seed"
    read = lambda n: (seed / n).read_text().strip() if (seed / n).exists() else ""
    return {"GA_FROZEN_NOW": read("frozen_now"), "GA_TAPE_MODE": read("tape_mode") or "replay",
            "GA_FAULTS": read("faults")}


def compose(*args: str, check: bool = True) -> str:
    import os
    r = subprocess.run(["docker", "compose", *args], check=False, cwd=STAND, capture_output=True, text=True,
                       env={**os.environ, **env()})
    if check and r.returncode:
        raise RuntimeError(f"compose {' '.join(args)}:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return r.stdout + r.stderr


def sql(q: str) -> list[list[str]]:
    out = compose("exec", "-T", "postgres", "psql", "-U", "ga", "-d", "gameanalytics", "-AtF", "\t", "-c", q)
    return [line.split("\t") for line in out.strip().splitlines() if line]


def one(q: str) -> str:
    rows = sql(q)
    return rows[0][0] if rows and rows[0] else ""


def last_run() -> dict:
    row = sql("select status, stats::text from pipeline_runs where kind='hourly' order by started_at desc limit 1")
    return {"status": row[0][0], **json.loads(row[0][1])} if row else {}


def drain(timeout_s: int = 600) -> float:
    t0, idle = time.time(), 0
    while idle < 3:
        if time.time() - t0 > timeout_s:
            raise TimeoutError(f"queue not empty after {timeout_s}s")
        time.sleep(4)
        queued = compose("exec", "-T", "redis", "redis-cli", "ZCARD", "arq:queue").strip()
        running = compose("exec", "-T", "redis", "redis-cli", "--scan", "--pattern", "arq:in-progress:*").strip()
        idle = idle + 1 if queued == "0" and not running else 0
    return time.time() - t0


@dataclass
class Scenario:
    name: str
    faults: str
    why: str
    checks: list = field(default_factory=list)  # (description, callable(ctx) -> bool)
    action: object = None  # instead of a plain inline cycle: callable(ctx) that breaks infrastructure mid-run


def http(path: str, method: str = "GET", headers: dict | None = None) -> int:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://localhost:8001{path}", method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def kill_worker_mid_run(ctx):
    compose("exec", "-T", "worker", "python", "-m", "app.cli", "enqueue", "hourly")
    time.sleep(4)  # inside the slowed top request
    compose("kill", "worker")
    ctx["after_kill"] = one("select status from pipeline_runs order by started_at desc limit 1")
    compose("start", "worker")
    for _ in range(30):
        time.sleep(1)
        if "worker.start" in compose("logs", "--since", "20s", "worker"):
            break
    time.sleep(2)


def restart_postgres_mid_run(ctx):
    compose("exec", "-T", "worker", "python", "-m", "app.cli", "enqueue", "hourly")
    time.sleep(3)
    compose("restart", "postgres")
    time.sleep(25)


def redis_down(ctx):
    compose("stop", "redis")
    ctx["list"] = http("/api/games?limit=1")
    ctx["deep"] = http("/api/games/ratatan/deep-analyze", "POST", {"X-Client-Id": "chaos"})
    ctx["deep_row"] = one("select status from deep_analyses order by id desc limit 1")
    compose("start", "redis")
    for _ in range(60):
        time.sleep(2)
        if "worker.start" in compose("logs", "--since", "30s", "worker"):
            break
    ctx["worker_back"] = "Up" in compose("ps", "worker")


def baseline(ctx):
    return ctx["run"]["status"] == "done" and ctx["misses"] == 0


SCENARIOS = [
    Scenario("baseline", "", "the recorded cycle replays cleanly", [
        ("run done, no tape misses", baseline),
    ]),
    Scenario("top_down", "metacritic@discovery.top:http503", "the New Releases request fails, the rest works", [
        ("run is partial and names the top stage", lambda c: c["run"]["status"] == "partial"
         and "top" in c["run"].get("failed", {})),
        ("catalog discovery still ran", lambda c: c["run"]["discovered"].get("catalog", 0) > 0),
        ("yesterday's top is kept", lambda c: int(one("select count(*) from games where top_rank is not null")) == 20),
    ]),
    Scenario("metacritic_down", "metacritic:http503", "Metacritic answers 503 to everything", [
        ("run finishes, not stuck", lambda c: c["run"]["status"] in ("partial", "error")),
        ("top is kept", lambda c: int(one("select count(*) from games where top_rank is not null")) == 20),
        ("no run left running", lambda c: one("select count(*) from pipeline_runs where status='running'") == "0"),
    ]),
    Scenario("metacritic_flaky", "metacritic:http503:0.3; metacritic:timeout:0.1", "a third of requests fail", [
        ("run finishes without lost games", lambda c: c["run"]["status"] == "done" and c["run"]["errors"] == 0),
    ]),
    Scenario("cloudflare", "metacritic:html:0.3", "some answers are a Cloudflare page with 200", [
        ("run finishes", lambda c: c["run"]["status"] in ("done", "partial")),
        ("no game lost its title or got empty fields",
         lambda c: one("select count(*) from games where title is null or title = ''") == "0"),
    ]),
    Scenario("schema_change", "metacritic:schema:0.3", "Metacritic changes its JSON: expected fields disappear", [
        ("run finishes", lambda c: c["run"]["status"] in ("done", "partial")),
        ("no run left running", lambda c: one("select count(*) from pipeline_runs where status='running'") == "0"),
    ]),
    Scenario("gemini_daily", "gemini:daily", "the free Gemini key is out of quota for the day", [
        ("queue drains in minutes, nobody sleeps for hours", lambda c: c["drain_s"] < 300),
        ("no analysis saved, the game is retried next cycle",
         lambda c: one(f"select count(*) from analyses where created_at > '{c['started']}'") == "0"),
    ]),
    Scenario("gemini_flaky", "gemini:http429:0.5:4; gemini-embed:http429:1:1", "per-minute limits on generation", [
        ("analyses still saved", lambda c: int(one(
            f"select count(*) from analyses where created_at > '{c['started']}'")) > 0),
    ]),
    Scenario("worker_killed", "metacritic@discovery.top:http503:1:3", "the worker dies in the middle of a cycle", [
        ("the run was left running by the kill", lambda c: c["after_kill"] == "running"),
        ("the restarted worker closes it at once", lambda c: one(
            f"select count(*) from pipeline_runs where status = 'interrupted' and started_at > '{c['started']}'")
         == "1"),
        ("and re-runs the lost job now, not in an hour", lambda c: c["drain_s"] < 120 and one(
            "select status from pipeline_runs order by started_at desc limit 1") in ("done", "partial")),
    ], action=kill_worker_mid_run),
    Scenario("postgres_restart", "metacritic@discovery.top:http503:1:3", "Postgres restarts during a cycle", [
        ("the run reconnects and finishes", lambda c: one(
            "select status from pipeline_runs order by started_at desc limit 1") in ("done", "partial")),
    ], action=restart_postgres_mid_run),
    Scenario("redis_down", "", "Redis is gone: reads still work, new jobs are refused honestly", [
        ("lists and cards still served", lambda c: c["list"] == 200),
        ("deep analyze answers 503", lambda c: c["deep"] == 503),
        ("and gives the slot back", lambda c: c["deep_row"] == "lost"),
        ("the worker comes back by itself", lambda c: c["worker_back"]),
    ], action=redis_down),
    Scenario("gemini_broken", "gemini:empty:0.5; gemini:badjson:0.5", "answers without text or with broken JSON", [
        ("queue drains", lambda c: c["drain_s"] < 300),
        ("no half-written analysis", lambda c: one(
            f"select count(*) from analyses where created_at > '{c['started']}' and payload = '{{}}'::jsonb") == "0"),
    ]),
]


def run(sc: Scenario) -> bool:
    print(f"\n== {sc.name}: {sc.why}  [{sc.faults or 'no faults'}]")
    sh(SH, "faults", sc.faults or "off")
    sh(SH, "reset")
    started = one("select now()")
    ctx: dict = {"started": started}
    if sc.action:
        sc.action(ctx)
        out = ""
    else:
        out = compose("exec", "-T", "worker", "python", "-m", "app.cli", "inline", "hourly", check=False)
    try:
        drain_s = drain()
    except TimeoutError as e:
        print(f"   FAIL  {e}")
        return False
    logs = compose("logs", "--since", "10m", "worker")
    ctx.update({"run": last_run(), "drain_s": drain_s, "crashed": "Traceback" in out,
                "misses": logs.count("tape miss") + out.count("tape miss")})
    print(f"   run: {ctx['run'].get('status')} failed={ctx['run'].get('failed')} errors={ctx['run'].get('errors')} "
          f"drain={drain_s:.0f}s cli_traceback={ctx['crashed']}")
    ok = True
    for desc, check in sc.checks:
        try:
            passed = bool(check(ctx))
        except Exception as e:  # noqa: BLE001 - a check that cannot run is a failed check
            passed, desc = False, f"{desc} ({type(e).__name__}: {e})"
        print(f"   {'PASS' if passed else 'FAIL'}  {desc}")
        ok &= passed
    return ok


def main() -> None:
    if "--list" in sys.argv:
        for sc in SCENARIOS:
            print(f"{sc.name:18} {sc.why}")
        return
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    picked = [sc for sc in SCENARIOS if not wanted or sc.name in wanted]
    sh(SH, "mode", "replay")
    results = {sc.name: run(sc) for sc in picked}
    sh(SH, "faults", "off")
    print("\n" + "\n".join(f"{'PASS' if v else 'FAIL'}  {k}" for k, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
