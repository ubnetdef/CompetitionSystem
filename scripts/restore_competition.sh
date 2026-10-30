#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$#" -ne 5 ]]; then
  echo "Usage: restore_competition.sh PROJECT_DIR DUMP_PATH KEYS_PATH BACKUP_DIR TARGET_LABEL" >&2
  exit 2
fi

PROJECT_DIR="$1"
IMPORT_DUMP="$2"
KEYS_FILE="$3"
BACKUP_DIR="$4"
TARGET_LABEL="$5"
STAGE_DIR="$(dirname "$IMPORT_DUMP")"

if [[ "$STAGE_DIR" != "$HOME"/.competition-import-* ||
      "$KEYS_FILE" != "$STAGE_DIR/import-keys.env" ||
      ! -s "$IMPORT_DUMP" ||
      ! -s "$KEYS_FILE" ]]; then
  echo "The staged import files are missing or outside the expected private staging directory." >&2
  exit 1
fi

ENV_TEMP=""
cleanup() {
  if [[ -n "$ENV_TEMP" ]]; then
    rm -f -- "$ENV_TEMP"
  fi
  rm -f -- "$IMPORT_DUMP" "$KEYS_FILE"
}
trap cleanup EXIT

chmod 600 "$IMPORT_DUMP" "$KEYS_FILE"
if [[ ! -d "$PROJECT_DIR" || ! -f "$PROJECT_DIR/docker-compose.yml" || ! -f "$PROJECT_DIR/.env" ]]; then
  echo "The target project, docker-compose.yml, or .env file was not found." >&2
  exit 1
fi

ENV_FILE="$PROJECT_DIR/.env"
if [[ ! "$BACKUP_DIR" == "$HOME/"* ]]; then
  echo "The backup directory must be under the target user's home directory." >&2
  exit 1
fi

secret_key="$(sed -n 's/\r$//;s/^SECRET_KEY=//p' "$KEYS_FILE")"
credential_key="$(sed -n 's/\r$//;s/^CHECK_CREDENTIAL_KEY=//p' "$KEYS_FILE")"
if [[ "$credential_key" == \"*\" ]]; then
  credential_key="${credential_key:1:${#credential_key}-2}"
elif [[ "$credential_key" == \'*\' ]]; then
  credential_key="${credential_key:1:${#credential_key}-2}"
fi
if [[ -z "$secret_key" || ! "$credential_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "The imported platform or credential-encryption keys are invalid." >&2
  exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
DATABASE_BACKUP="$BACKUP_DIR/competition-before-import-$timestamp.dump"
ENV_BACKUP="$BACKUP_DIR/env-before-import-$timestamp"

if (( EUID == 0 )); then
  run_compose() { docker compose "$@"; }
else
  sudo -v
  run_compose() { sudo docker compose "$@"; }
fi

cd "$PROJECT_DIR"
run_compose up -d database

database_ready=false
for attempt in $(seq 1 45); do
  if run_compose exec -T database pg_isready -U competition -d competition >/dev/null 2>&1; then
    database_ready=true
    break
  fi
  sleep 2
done
if [[ "$database_ready" != true ]]; then
  echo "The target PostgreSQL database did not become ready." >&2
  run_compose logs --tail=80 database >&2
  exit 1
fi

echo "Backing up the current VM database to $DATABASE_BACKUP."
if ! run_compose exec -T database pg_dump -Fc -U competition -d competition > "$DATABASE_BACKUP"; then
  echo "Could not back up the VM database; refusing to import." >&2
  exit 1
fi
if [[ ! -s "$DATABASE_BACKUP" ]]; then
  echo "The VM database backup is empty; refusing to import." >&2
  exit 1
fi
cp -p "$ENV_FILE" "$ENV_BACKUP"
chmod 600 "$DATABASE_BACKUP" "$ENV_BACKUP"

expected_confirmation="REPLACE $TARGET_LABEL"
printf 'A VM database backup is ready at %s.\n' "$DATABASE_BACKUP"
read -r -p "Type '$expected_confirmation' to replace the VM database: " confirmation </dev/tty
if [[ "$confirmation" != "$expected_confirmation" ]]; then
  echo "Import cancelled. The VM database was not changed; its backup was retained."
  exit 1
fi

ENV_TEMP="$(mktemp "$PROJECT_DIR/.env.import.XXXXXX")"
awk -F= '
  FNR == NR {
    sub(/\r$/, "", $0)
    separator = index($0, "=")
    if (separator > 0) {
      imported[substr($0, 1, separator - 1)] = substr($0, separator + 1)
    }
    next
  }
  {
    sub(/\r$/, "", $0)
    key = $1
    if (key == "SECRET_KEY" || key == "CHECK_CREDENTIAL_KEY") {
      if (!seen[key]++) {
        print key "=" imported[key]
      }
    } else {
      print $0
    }
  }
  END {
    for (key in imported) {
      if (!seen[key]) {
        print key "=" imported[key]
      }
    }
  }
' "$KEYS_FILE" "$ENV_FILE" > "$ENV_TEMP"
chmod 600 "$ENV_TEMP"
mv -- "$ENV_TEMP" "$ENV_FILE"
ENV_TEMP=""

echo "Stopping the web app and scoring worker before replacing the database."
if ! run_compose stop scoring-worker web; then
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  echo "Could not stop the application services; the original environment was restored." >&2
  run_compose up -d web scoring-worker
  exit 1
fi

restore_database() {
  local archive="$1"
  run_compose exec -T database dropdb -U competition --if-exists competition || return 1
  run_compose exec -T database createdb -U competition -O competition competition || return 1
  run_compose exec -T database pg_restore \
    --no-owner --no-acl --exit-on-error --single-transaction \
    -U competition -d competition < "$archive" || return 1
}

echo "Importing the full local database snapshot."
if ! restore_database "$IMPORT_DUMP"; then
  echo "The import failed; restoring the pre-import VM database and environment." >&2
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  if restore_database "$DATABASE_BACKUP"; then
    run_compose up -d --build web scoring-worker
    echo "The original VM database was restored. The import did not complete." >&2
  else
    echo "Automatic rollback failed. Keep the database backup at $DATABASE_BACKUP and restore it manually." >&2
  fi
  exit 1
fi

if ! run_compose exec -T database psql -U competition -d competition -v ON_ERROR_STOP=1 <<'SQL'
UPDATE score_rounds
SET status = 'failed',
    error_message = COALESCE(error_message, 'Round was interrupted during database import.'),
    completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
WHERE status = 'running';
SQL
then
  echo "The database was imported, but interrupted rounds could not be marked failed." >&2
  run_compose up -d --build web scoring-worker
  exit 1
fi

echo "Starting the web app and scoring worker with the imported data."
run_compose up -d --build web scoring-worker

application_ready=false
for attempt in $(seq 1 60); do
  if run_compose exec -T web flask --app app:create_app db-check >/dev/null 2>&1; then
    application_ready=true
    break
  fi
  sleep 2
done
if [[ "$application_ready" != true ]]; then
  echo "The imported database is in place, but the web app did not become ready. Backups are retained." >&2
  run_compose logs --tail=80 web database scoring-worker >&2
  exit 1
fi

run_compose ps
echo "Competition data import completed. Backups are retained in $BACKUP_DIR."
