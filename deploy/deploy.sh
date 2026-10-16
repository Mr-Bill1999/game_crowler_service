#!/usr/bin/env bash
# Deploy GameAnalytics to one VPS from this machine, over SSH with a key (never a password).
#
#   deploy/deploy.sh check            what the server is: OS, CPU, memory, disk, Docker
#   deploy/deploy.sh install-docker   Docker Engine + Compose plugin from the distribution's packages
#   deploy/deploy.sh up               ship the committed code, write deploy/.env, build and start the stack
#   deploy/deploy.sh db-copy          replace the server's database with a dump of the local working one
#   deploy/deploy.sh status | logs [service] | backup
#
# Settings: GA_HOST or deploy/.host (user@server, kept out of git), GA_KEY (default ~/.ssh/ga_deploy),
# GA_DIR (default /opt/gameanalytics)
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${GA_HOST:-$(cat deploy/.host 2>/dev/null || true)}
[[ -n $HOST ]] || { echo "set GA_HOST=user@server or write it to deploy/.host" >&2; exit 1; }
KEY=${GA_KEY:-$HOME/.ssh/ga_deploy}
DIR=${GA_DIR:-/opt/gameanalytics}
SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$HOST")
DC="docker compose -f $DIR/deploy/compose.yml"

remote() { "${SSH[@]}" "$@"; }

check() {
  remote 'set -e; . /etc/os-release; echo "os: $PRETTY_NAME"; echo "cpu: $(nproc)"; free -h | awk "/Mem:/{print \"memory: \"\$2\" total, \"\$7\" available\"}";
          df -h / | awk "NR==2{print \"disk: \"\$4\" free of \"\$2}"; (docker --version && docker compose version) 2>/dev/null || echo "docker: not installed";
          ss -ltnp 2>/dev/null | awk "NR>1{print \"listening: \"\$4}" | sort -u | head -20'
}

install_docker() {
  remote 'set -e; command -v docker >/dev/null && docker compose version >/dev/null && { echo "docker already installed"; exit 0; }
          export DEBIAN_FRONTEND=noninteractive; apt-get update -qq
          apt-get install -y -qq docker.io docker-compose-v2 || apt-get install -y -qq docker.io docker-compose-plugin
          systemctl enable --now docker; docker --version; docker compose version'
}

write_env() {
  # Only what the server needs from backend/.env; local database and Redis addresses stay local.
  local keys
  keys=$(grep -E '^GA_(GEMINI_API_KEY|YOUTUBE_API_KEY|LLM_PROVIDER|LLM_MODEL|LLM_THINKING_LEVEL)=' backend/.env || true)
  printf '%s\n' "$keys" | remote "set -e; umask 077; mkdir -p $DIR/deploy
    old=\$(grep -E '^POSTGRES_PASSWORD=' $DIR/deploy/.env 2>/dev/null || true)
    pw=\${old:-POSTGRES_PASSWORD=\$(openssl rand -hex 24)}
    { echo \"\$pw\"; cat; } > $DIR/deploy/.env.new && mv $DIR/deploy/.env.new $DIR/deploy/.env
    chmod 600 $DIR/deploy/.env; echo \"deploy/.env: \$(grep -c = $DIR/deploy/.env) settings, values not shown\""
}

up() {
  git diff --quiet HEAD -- backend frontend deploy || echo "warning: uncommitted changes are not shipped, only HEAD $(git rev-parse --short HEAD)"
  remote "mkdir -p $DIR"
  git archive --format=tar HEAD backend frontend deploy | remote "tar -x -C $DIR"
  write_env
  remote "set -e; mkdir -p $DIR/deploy/backups; cd $DIR && $DC up -d --build --remove-orphans && $DC ps"
}

db_copy() {
  local dump=/tmp/ga-$(date +%s).dump
  docker exec gameanalytics-postgres-1 pg_dump -U ga -Fc gameanalytics > "$dump"
  echo "local dump: $(du -h "$dump" | cut -f1)"
  scp -i "$KEY" -o BatchMode=yes "$dump" "$HOST:$DIR/deploy/backups/initial.dump"
  rm -f "$dump"
  remote "set -e; cd $DIR; $DC stop worker api
    $DC exec -T postgres psql -U ga -d postgres -qc 'DROP DATABASE IF EXISTS gameanalytics WITH (FORCE)'
    $DC exec -T postgres createdb -U ga gameanalytics
    $DC exec -T postgres pg_restore -U ga -d gameanalytics --no-owner /backups/initial.dump
    $DC exec -T redis redis-cli FLUSHALL >/dev/null
    $DC start api worker
    $DC exec -T postgres psql -U ga -d gameanalytics -Atc 'select count(*) from games'"
}

backup() {
  remote "set -e; cd $DIR; f=/backups/ga-\$(date +%F-%H%M).dump
    $DC exec -T postgres pg_dump -U ga -Fc gameanalytics -f \$f && ls -lh deploy/backups | tail -3
    find deploy/backups -name 'ga-*.dump' -mtime +14 -delete"
}

case ${1:-help} in
  check) check ;;
  install-docker) install_docker ;;
  up) up ;;
  db-copy) db_copy ;;
  status) remote "cd $DIR && $DC ps" ;;
  logs) remote "cd $DIR && $DC logs --tail 100 ${2:-}" ;;
  backup) backup ;;
  *) sed -n '2,12p' "$0" ;;
esac
