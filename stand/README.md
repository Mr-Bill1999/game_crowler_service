# Test stand

The full stack (api, worker, web, Postgres, Redis) as its own Docker Compose project `ga-stand`, apart from the
working service: its own ports, database and queues, so it can be broken on purpose without touching real data.

| Service  | Stand          | Working service |
|----------|----------------|-----------------|
| web      | localhost:8081 | localhost:5173  |
| api      | localhost:8001 | localhost:8000  |
| Postgres | localhost:5433 | localhost:5432  |
| Redis    | localhost:6380 | localhost:6379  |

## Reproducible runs

Two things make a run on the stand repeat exactly:

- **Snapshot and frozen clock.** `stand.sh seed` dumps the working database into `stand/seed/` and freezes the clock
  at that moment (`GA_FROZEN_NOW`). Discovery windows, refresh tiers and quota days are computed from that clock,
  so every run from the snapshot asks the same questions. Cron is off: cycles run on command.
- **Tapes.** Every call to Metacritic, Gemini (generation and embeddings) and YouTube goes through
  `app/core/tape.py`. In `record`/`auto` mode the answers are saved under `stand/tapes/`, in `replay` mode they are
  served from there and a call nobody recorded fails with `TapeMiss` instead of reaching the service.
  API keys are never part of a tape: Metacritic needs none, Gemini and YouTube keys travel in headers.

The free Gemini key is shared with the working service. Replay costs nothing; record and live runs spend the same
daily quota, so plan them.

## Commands

```bash
stand/stand.sh seed      # snapshot the working database, freeze the clock
stand/stand.sh up        # build and start
stand/stand.sh record    # reset to the snapshot + one hourly cycle, recording what is missing
stand/stand.sh replay    # reset + the same cycle from tapes only; the report must show 0 tape misses
stand/stand.sh report    # counts, tapes per source, tape misses since the last cycle
stand/stand.sh mode live # or record | replay | auto
stand/stand.sh down      # stop; `down -v` also drops the stand's database volume
```

A new snapshot changes the frozen clock, so old tapes stop matching: re-record after `seed`.
`stand/seed/` and `stand/tapes/` hold Metacritic data and model output and stay out of git.

## Breaking it on purpose

```bash
stand/stand.sh faults "metacritic:http503:0.3; gemini:daily"   # see backend/app/core/faults.py
stand/stand.sh faults off
python3 stand/chaos.py           # every scenario: reset, break, run a cycle, check; exit 1 on a failed check
python3 stand/chaos.py --list
backend/.venv/bin/python stand/load.py --users 50 --seconds 30 --sse 30
GA_TEST_DATABASE_URL=postgresql+asyncpg://ga:ga@localhost:5433/ga_test backend/.venv/bin/python -m pytest backend/tests/integration
```

Findings and their regression checks: [FINDINGS.md](FINDINGS.md).
