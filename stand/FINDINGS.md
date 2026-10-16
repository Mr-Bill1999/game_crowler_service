# Phase 1: breaking the service — findings

Stand `ga-stand`, 16.10.2026. Every finding was fixed and has a regression check:
`backend/tests` (unit), `backend/tests/integration` (real Postgres, `GA_TEST_DATABASE_URL`),
`stand/chaos.py` (whole pipeline on the stand), `stand/load.py` (API under load).

| # | What broke | Fix | Regression check |
|---|---|---|---|
| 1 | nginx kept the api container's old address: after an api restart the site answered 502 until nginx restarted | resolve `api` through Docker DNS on every request | manual: api restart → 200 before and after |
| 2 | an empty `GA_FROZEN_NOW` (or empty key) from Compose crashed settings at startup | empty env values mean "not set" | stand starts with the variables empty |
| 3 | a failed top-20 request aborted the whole hourly cycle: no catalog discovery, no refreshes | stages run independently; the run is `partial` and lists what failed; yesterday's top is kept | chaos `top_down`, `metacritic_down`, `schema_change` |
| 4 | Gemini's daily limit answers "retry in 11 h": the worker would sleep that long holding a job slot | a retryDelay over 3 min fails at once; the next cycle retries | unit `test_gemini_daily_limit_fails_fast...`, chaos `gemini_daily` |
| 5 | a 200 with a Cloudflare page or unexpected JSON was taken as data | bodies are checked; such answers are retried like a 5xx | unit `test_cloudflare_page...`, chaos `cloudflare` |
| 6 | YouTube answering HTML or timing out raised an unhandled error and left the deep analysis "running" | all YouTube failures become `search_error` (free) | unit `test_youtube_quota_and_broken_bodies...` |
| 7 | a Deep analyze that failed on Gemini's side still used the user's daily slot | failed analyses are free (`failed`), the user can retry | covered by quota statuses |
| 8 | a worker killed mid-cycle left the run "running" forever | the worker closes its own lost runs at startup; CLI runs only after the job timeout | integration `test_a_restarted_worker...`, chaos `worker_killed` |
| 9 | Deep analyses left queued by a killed worker or a lost queue held a pool slot all day | pending for 30 min → `lost` (free); if Redis is down at enqueue time the API answers 503 and frees the slot | integration `test_lost_deep_analyses...`, `test_deep_analyze_with_the_queue_down...`, chaos `redis_down` |
| 10 | containers did not come back after a Docker or host restart (the outage of 15.10) | `restart: unless-stopped` on every service | `docker inspect` on the stand |
| 11 | the DevPanel event stream dropped into a reconnect loop when Redis was unreachable | the stream stays open with keepalives and resumes when Redis is back | manual on the stand: Redis off → on in one connection |
| 12 | `limit=-1`, `offset=-5`, `history=-1` answered 500 | bounds on every list parameter → 422 | integration `test_list_parameters_out_of_range...` |
| 13 | search treated `%` and `_` as wildcards: `q=%` returned every game | LIKE wildcards are escaped, query capped at 200 chars | integration `test_search_text_is_text_not_a_pattern` |
| 14 | a made-up `X-Client-Id` per request gave unlimited Deep analyses up to the whole daily pool | a second cap per address (3 a day), taken from nginx's X-Real-IP | integration `test_made_up_client_ids...` |
| 15 | quota day boundaries were Pacific-time datetimes: subtracting them across a clock change gives 24 h for a 25-hour day | boundaries are returned in UTC | unit `test_quota_day_on_the_autumn_clock_change` and two more |
| 16 | jobs a killed worker was running stayed "in progress" in the arq queue until their timeout: a lost hourly cycle waited 55 minutes, a manual run was blocked as long | the worker drops stale in-progress marks at startup (one worker per queue), lost jobs run again at once | chaos `worker_killed` |

Survived without changes: Metacritic 503 on a third of requests and timeouts (retries), a Postgres restart in the
middle of a cycle (connections recover), Gemini per-minute limits, empty and broken Gemini answers (nothing
half-written is saved), the transition to winter time on 1 November 2026.

## Load

Through nginx on the stand, generators on the same Mac (they share its cores with Docker, so this is a floor):

| API processes | users | streams | req/s | p50 | p95 | errors |
|---|---|---|---|---|---|---|
| 1 | 50 | 30 | 364 | 112–146 ms | 153–192 ms | 0 |
| 1 | 200 | 0 | ~350 (API at 100% of one core) | ~600 ms | 0.75–1.4 s | 0 |
| 4 | 200 | 0 | ~446 | ~300 ms | 1.3–1.4 s | 0 |

One process tops out near 350 req/s; the number of processes is `GA_API_WORKERS` (default 2).
Far beyond what the MVP needs; size the server in phase 3.

## Not covered yet

- A frozen clock right before midnight UTC on the stand (needs new tapes); the window logic is covered by unit tests.
- A full Docker daemon restart (would stop the working service too); the restart policy is checked instead.
