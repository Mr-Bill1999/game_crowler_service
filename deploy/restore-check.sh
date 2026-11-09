#!/usr/bin/env bash
# Weekly proof that the newest backup restores: into a scratch database next to the live one, then a count of the
# games against the live database, then the scratch database is dropped. Runs from ga-restore-check.timer
# (Sunday 05:30 UTC); the result goes to Redis for the health check and DevPanel.
set -uo pipefail
DIR=${GA_DIR:-/opt/gameanalytics}
DC="docker compose -f $DIR/deploy/compose.yml"
OUT=$DIR/deploy/backups
SCRATCH=ga_restore_check

report() {  # ok detail [games]
  $DC exec -T redis redis-cli HSET health:restore at "$(date -u +%FT%T+00:00)" ok "$1" detail "$2" games "${3:-0}" \
    >/dev/null
  echo "restore check: ok=$1 $2"
}

latest=$(find "$OUT/daily" -name 'ga-*.dump' | sort -r | head -1)
[ -n "$latest" ] || { report 0 "no backup in $OUT/daily"; exit 1; }
file=${latest#"$OUT"/}

$DC exec -T postgres psql -U ga -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH" &&
  $DC exec -T postgres createdb -U ga "$SCRATCH" || { report 0 "could not create $SCRATCH"; exit 1; }
if ! $DC exec -T postgres pg_restore -U ga -d "$SCRATCH" --no-owner --exit-on-error "/backups/$file" 2>/tmp/ga-restore.log
then
  report 0 "pg_restore failed on $file: $(tail -c 200 /tmp/ga-restore.log | tr '\n' ' ')"
  $DC exec -T postgres psql -U ga -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH"
  exit 1
fi
restored=$($DC exec -T postgres psql -U ga -d "$SCRATCH" -Atc 'select count(*) from games')
live=$($DC exec -T postgres psql -U ga -d gameanalytics -Atc 'select count(*) from games')
$DC exec -T postgres psql -U ga -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH"
# the dump is at most a day old: the live catalogue may have grown since, but not by a tenth
if [ "$restored" -gt 0 ] && [ $((restored * 10)) -ge $((live * 9)) ]; then
  report 1 "$file: $restored games restored, $live live" "$restored"
else
  report 0 "$file: only $restored games restored, $live live" "$restored"
  exit 1
fi
