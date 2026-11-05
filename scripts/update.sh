#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run the updater with sudo: sudo bash scripts/update.sh" >&2
  exit 1
fi

if [[ ! -f docker-compose.yml || ! -f Dockerfile || ! -f requirements.txt ]]; then
  echo "Run the updater from a complete CompetitionSystem checkout." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "The .env file is missing; the updater will not create or replace it." >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1 || [[ ! -d .git ]]; then
  echo "A Git checkout and Git CLI are required to update this installation." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required; run scripts/setup.sh to install prerequisites." >&2
  exit 1
fi

REPO_USER="${SUDO_USER:-$(stat -c '%U' "$PROJECT_DIR")}"
if [[ -z "$REPO_USER" ]] || ! id "$REPO_USER" >/dev/null 2>&1; then
  echo "Cannot identify the account that owns this checkout." >&2
  exit 1
fi

run_repo_git() {
  if [[ "$REPO_USER" != root ]]; then
    runuser -u "$REPO_USER" -- git -C "$PROJECT_DIR" "$@"
  else
    git -C "$PROJECT_DIR" "$@"
  fi
}

if [[ -n "$(run_repo_git status --porcelain)" ]]; then
  echo "The checkout has local changes. Commit or safely preserve them before updating." >&2
  exit 1
fi
if run_repo_git ls-files --error-unmatch -- .env >/dev/null 2>&1; then
  echo "The .env file is tracked by Git. Untrack it before updating so a pull cannot replace its secrets." >&2
  exit 1
fi

echo "Fast-forwarding the checkout as $REPO_USER."
if ! run_repo_git pull --ff-only; then
  echo "Git update failed; no application containers or database data were changed." >&2
  exit 1
fi

if ! docker compose config --quiet; then
  echo "Compose configuration is invalid; the existing services were not rebuilt." >&2
  exit 1
fi

BACKUP_HOME="$(getent passwd "$REPO_USER" | cut -d: -f6)"
if [[ -z "$BACKUP_HOME" ]]; then
  echo "Cannot determine the home directory for $REPO_USER." >&2
  exit 1
fi
BACKUP_DIR="${UPDATE_BACKUP_DIR:-$BACKUP_HOME/competition-backups/updates}"
REPO_UID="$(id -u "$REPO_USER")"
REPO_GID="$(id -g "$REPO_USER")"
install -d -m 700 -o "$REPO_UID" -g "$REPO_GID" "$BACKUP_DIR"

echo "Starting the existing database service without removing its volume."
docker compose up -d database

database_ready=false
for attempt in $(seq 1 45); do
  if docker compose exec -T database pg_isready -U competition -d competition >/dev/null 2>&1; then
    database_ready=true
    break
  fi
  sleep 2
done
if [[ "$database_ready" != true ]]; then
  echo "The database did not become ready; the application was not updated." >&2
  docker compose logs --tail=80 database >&2 || true
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
DATABASE_BACKUP="$BACKUP_DIR/competition-before-update-$timestamp.dump"
echo "Backing up the database before app migrations to $DATABASE_BACKUP."
if ! docker compose exec -T database pg_dump -Fc -U competition -d competition > "$DATABASE_BACKUP"; then
  rm -f -- "$DATABASE_BACKUP"
  echo "Database backup failed; application services were not rebuilt." >&2
  exit 1
fi
if [[ ! -s "$DATABASE_BACKUP" ]]; then
  rm -f -- "$DATABASE_BACKUP"
  echo "The database backup is empty; application services were not rebuilt." >&2
  exit 1
fi
chmod 600 "$DATABASE_BACKUP"
chown "$REPO_UID:$REPO_GID" "$DATABASE_BACKUP"

proxy_container="$(docker compose --profile public ps -q proxy)"
if [[ -n "$proxy_container" ]]; then
  echo "Rebuilding the web app, scoring worker, and already-running public proxy."
  docker compose --profile public up -d --build web scoring-worker proxy
else
  echo "Rebuilding the web app and scoring worker; the public proxy profile remains unchanged."
  docker compose up -d --build web scoring-worker
fi

web_ready=false
for attempt in $(seq 1 45); do
  if docker compose exec -T web flask --app app:create_app db-check >/dev/null 2>&1 &&
    docker compose exec -T web python -c \
      "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=5).read()" \
      >/dev/null 2>&1; then
    web_ready=true
    break
  fi
  sleep 2
done
if [[ "$web_ready" != true ]]; then
  echo "The web app did not become ready. The database backup is retained at $DATABASE_BACKUP." >&2
  docker compose logs --tail=80 web database >&2 || true
  exit 1
fi

worker_ready=false
for attempt in $(seq 1 45); do
  worker_container="$(docker compose ps -q scoring-worker | head -n 1)"
  if [[ -n "$worker_container" ]] &&
    [[ "$(docker inspect --format '{{.State.Health.Status}}' "$worker_container" 2>/dev/null || true)" == healthy ]] &&
    docker compose exec -T scoring-worker flask --app app:create_app db-check >/dev/null 2>&1; then
    worker_ready=true
    break
  fi
  sleep 2
done
if [[ "$worker_ready" != true ]]; then
  echo "The scoring worker did not become ready. The database backup is retained at $DATABASE_BACKUP." >&2
  docker compose logs --tail=80 web database scoring-worker >&2 || true
  exit 1
fi

docker compose ps
printf '\nUpdate complete. The .env file and Docker volumes were left in place.\nDatabase backup: %s\n' "$DATABASE_BACKUP"
