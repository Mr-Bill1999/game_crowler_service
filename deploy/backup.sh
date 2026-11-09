#!/usr/bin/env bash
# Nightly backup of the production database. Runs on the server from ga-backup.timer (deploy/systemd), 03:15 UTC:
# between the hourly cycles (they start at :00 and take 2-9 minutes) and the 04:30 catalogue sweep.
#
# The raw Metacritic answers are left out (the table is kept, its rows are not): they are three quarters of the
# database, and every one of them is fetched again with the next refresh of its game.
# Keeps 14 daily and 8 weekly (Sunday) dumps; reports to Redis, where the worker's health check reads it.
set -euo pipefail
DIR=${GA_DIR:-/opt/gameanalytics}
DC="docker compose -f $DIR/deploy/compose.yml"
OUT=$DIR/deploy/backups  # /backups inside the postgres container
mkdir -p "$OUT/daily" "$OUT/weekly"

name=daily/ga-$(date -u +%F).dump
$DC exec -T postgres pg_dump -U ga -Fc --exclude-table-data=raw_responses gameanalytics -f "/backups/$name.tmp"
mv "$OUT/$name.tmp" "$OUT/$name"
[ "$(date -u +%u)" = 7 ] && cp "$OUT/$name" "$OUT/weekly/"

find "$OUT/daily" -name 'ga-*.dump' -mtime +13 -delete
ls -1t "$OUT"/weekly/ga-*.dump 2>/dev/null | tail -n +9 | xargs -r rm -f

size=$(stat -c %s "$OUT/$name")
$DC exec -T redis redis-cli HSET health:backup at "$(date -u +%FT%T+00:00)" bytes "$size" file "$name" >/dev/null
echo "backup: $name, $((size / 1048576)) MB"
