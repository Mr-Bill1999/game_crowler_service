#!/usr/bin/env bash
# Copies the server's backups to this Mac, so a lost server is not a lost database. Run by launchd once a day
# (deploy/mac/com.gamegallery.backups.plist, installed by `deploy/deploy.sh install-mac-pull`); a Mac that was asleep
# at that hour runs it when it wakes. Keeps what the server keeps, plus anything older that is already here.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${GA_HOST:-$(cat deploy/.host)}
KEY=${GA_KEY:-$HOME/.ssh/ga_deploy}
DEST=${GA_BACKUP_DEST:-$HOME/GameGallery-backups}
mkdir -p "$DEST"
rsync -a -e "ssh -i $KEY -o BatchMode=yes" "$HOST:/opt/gameanalytics/deploy/backups/daily" \
  "$HOST:/opt/gameanalytics/deploy/backups/weekly" "$DEST/"
find "$DEST/daily" -name 'ga-*.dump' -mtime +30 -delete 2>/dev/null || true
echo "$(date '+%F %T') pulled: $(ls "$DEST"/daily | tail -1)"
