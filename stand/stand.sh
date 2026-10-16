#!/usr/bin/env bash
# Test stand driver. See stand/README.md.
#
#   stand/stand.sh up              build and start the stand (ports: api 8001, web 8081, postgres 5433, redis 6380)
#   stand/stand.sh seed            snapshot the working database into stand/seed and freeze the clock at that moment
#   stand/stand.sh reset           put the stand's database back to the snapshot, empty its queues
#   stand/stand.sh record          reset + one hourly cycle with GA_TAPE_MODE=auto: live calls that are missing get recorded
#   stand/stand.sh replay          reset + the same cycle with GA_TAPE_MODE=replay: no external call at all
#   stand/stand.sh mode <m>        restart api and worker in tape mode live|record|replay|auto
#   stand/stand.sh cycle           one hourly cycle (+ the analyses and facets it queues) in the current mode
#   stand/stand.sh report          what the stand holds now, tapes per source, tape misses
#   stand/stand.sh cli <args>      python -m app.cli <args> inside the stand
#   stand/stand.sh down            stop the stand (data volume kept; `down -v` removes it)
set -euo pipefail
cd "$(dirname "$0")"

SEED=seed/seed.dump
FROZEN=seed/frozen_now
MODE_FILE=seed/tape_mode

frozen_now() { [[ -f $FROZEN ]] && cat "$FROZEN" || true; }
tape_mode() { [[ -f $MODE_FILE ]] && cat "$MODE_FILE" || echo replay; }
dc() { GA_FROZEN_NOW="$(frozen_now)" GA_TAPE_MODE="$(tape_mode)" docker compose "$@"; }
psql_stand() { dc exec -T postgres psql -U ga -d gameanalytics -Atc "$1"; }

set_mode() {
  mkdir -p seed
  echo "$1" > "$MODE_FILE"
  dc up -d api worker >/dev/null
  echo "tape mode: $1, frozen clock: $(frozen_now || echo none)"
}

reset() {
  [[ -f $SEED ]] || { echo "no snapshot: run stand/stand.sh seed first" >&2; exit 1; }
  dc stop worker >/dev/null
  dc exec -T postgres psql -U ga -d postgres -qc "DROP DATABASE IF EXISTS gameanalytics WITH (FORCE)"
  dc exec -T postgres createdb -U ga gameanalytics
  dc exec -T postgres pg_restore -U ga -d gameanalytics --no-owner < "$SEED"
  dc exec -T redis redis-cli FLUSHALL >/dev/null
  dc restart api >/dev/null
  dc start worker >/dev/null
  echo "reset to snapshot taken at $(frozen_now)"
}

drain() {
  # arq keeps queued jobs in a sorted set and marks running ones with in-progress keys
  local idle=0
  while (( idle < 3 )); do
    sleep 5
    local queued running
    queued=$(dc exec -T redis redis-cli ZCARD arq:queue | tr -d '\r')
    running=$(dc exec -T redis redis-cli --scan --pattern 'arq:in-progress:*' | wc -l | tr -d ' ')
    if [[ $queued == 0 && $running == 0 ]]; then idle=$((idle + 1)); else idle=0; echo "  queued $queued, running $running"; fi
  done
}

cycle() {
  local since
  since=$(date -u +%Y-%m-%dT%H:%M:%S)
  echo "$since" > seed/last_cycle_at
  dc exec -T worker python -m app.cli inline hourly
  drain
}

report() {
  echo "== database"
  psql_stand "select 'games', count(*) from games union all select 'reviews', count(*) from reviews
              union all select 'facets', count(*) from game_facets union all select 'similar', count(*) from similar_games
              union all select 'deep', count(*) from deep_analyses" | column -t -s'|'
  echo "== analyses by kind and mode"
  psql_stand "select kind, mode, count(*), sum(tokens_in) from analyses group by 1, 2 order by 1, 2" | column -t -s'|'
  echo "== last run"
  psql_stand "select id, status, left(stats::text, 300) from pipeline_runs order by started_at desc limit 1"
  echo "== tapes"
  if [[ -d tapes ]]; then find tapes -name '*.json.gz' | cut -d/ -f2 | sort | uniq -c; else echo "  none"; fi
  echo "== tape misses since the last cycle"
  local since
  since=$(cat seed/last_cycle_at 2>/dev/null || echo 1970-01-01T00:00:00)
  dc logs --since "$since" worker 2>&1 | grep -c "tape miss" || true
}

cmd=${1:-help}
shift || true
case $cmd in
  up) dc up -d --build ;;
  seed)
    mkdir -p seed
    docker exec gameanalytics-postgres-1 pg_dump -U ga -Fc gameanalytics > "$SEED"
    date -u +%Y-%m-%dT%H:%M:00+00:00 > "$FROZEN"
    echo "snapshot $(du -h "$SEED" | cut -f1), clock frozen at $(frozen_now)"
    ;;
  reset) reset ;;
  mode) set_mode "${1:?mode: live|record|replay|auto}" ;;
  cycle) cycle ;;
  record) set_mode auto; reset; cycle; report ;;
  replay) set_mode replay; reset; cycle; report ;;
  report) report ;;
  cli) dc exec worker python -m app.cli "$@" ;;
  down) dc down "$@" ;;
  *) sed -n '2,15p' "$0" ;;
esac
