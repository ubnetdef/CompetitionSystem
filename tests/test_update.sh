#!/usr/bin/env bash
set -euo pipefail
trap 'status=$?; printf "Updater test failed (status %s): %s\n" "$status" "$BASH_COMMAND" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this updater test as root inside an Ubuntu 24.04 container." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "This updater test requires Ubuntu 24.04; found ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
PROJECT_DIR="$TEMP_DIR/project"
STUB_DIR="$TEMP_DIR/stubs"
LOG="$TEMP_DIR/commands.log"
mkdir -p "$PROJECT_DIR/scripts" "$PROJECT_DIR/.git" "$STUB_DIR"
cp "$ROOT_DIR/scripts/update.sh" "$PROJECT_DIR/scripts/update.sh"
cp "$ROOT_DIR/docker-compose.yml" "$PROJECT_DIR/docker-compose.yml"
cp "$ROOT_DIR/Dockerfile" "$PROJECT_DIR/Dockerfile"
cp "$ROOT_DIR/requirements.txt" "$PROJECT_DIR/requirements.txt"
printf 'POSTGRES_PASSWORD=test-value\nSECRET_KEY=test-value\n' > "$PROJECT_DIR/.env"
printf 'POSTGRES_PASSWORD=test-value\nSECRET_KEY=test-value\n' > "$TEMP_DIR/env.expected"

cat > "$STUB_DIR/git" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\n' "$*" >> "$TEST_COMMAND_LOG"
if [[ "$*" == *" status --porcelain" ]]; then
  if [[ "${TEST_GIT_DIRTY:-false}" == true ]]; then
    printf ' M README.md\n'
  fi
elif [[ "$*" == *" ls-files --error-unmatch -- .env" ]]; then
  if [[ "${TEST_ENV_TRACKED:-false}" == true ]]; then
    printf '.env\n'
  else
    exit 1
  fi
fi
STUB
chmod 700 "$STUB_DIR/git"

cat > "$STUB_DIR/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\n' "$*" >> "$TEST_COMMAND_LOG"
if [[ "$1" == compose && "${2:-}" == version ]]; then
  echo "Docker Compose version v2.test"
elif [[ "$1" == compose && "${2:-}" == --profile && "${3:-}" == public && "${4:-}" == ps && "${5:-}" == -q ]]; then
  if [[ "${TEST_PROXY_RUNNING:-false}" == true ]]; then
    echo "proxy-id"
  fi
elif [[ "$1" == compose && "${2:-}" == ps && "${3:-}" == -q ]]; then
  echo "worker-id"
elif [[ "$1" == inspect ]]; then
  echo "healthy"
elif [[ "$*" == *" pg_dump "* ]]; then
  echo "test-postgres-dump"
fi
STUB
chmod 700 "$STUB_DIR/docker"

export TEST_COMMAND_LOG="$LOG"
export UPDATE_BACKUP_DIR="$TEMP_DIR/backups"
export PATH="$STUB_DIR:$PATH"
export SUDO_USER=root

bash "$PROJECT_DIR/scripts/update.sh" > "$TEMP_DIR/update.out"
grep -Fq "test-postgres-dump" "$TEMP_DIR"/backups/competition-before-update-*.dump
grep -Fq "Database backup:" "$TEMP_DIR/update.out"
cmp "$PROJECT_DIR/.env" "$TEMP_DIR/env.expected"
grep -Fq "git -C $PROJECT_DIR pull --ff-only" "$LOG"
grep -Fq "docker compose exec -T database pg_dump -Fc -U competition -d competition" "$LOG"
grep -Fq "docker compose up -d --build web scoring-worker" "$LOG"
if grep -Eiq 'docker compose down|docker volume (rm|prune)|docker system prune' "$LOG"; then
  echo "The updater attempted a destructive Docker operation." >&2
  exit 1
fi

: > "$LOG"
if TEST_GIT_DIRTY=true bash "$PROJECT_DIR/scripts/update.sh" > "$TEMP_DIR/dirty.out" 2>&1; then
  echo "The updater accepted a checkout with local changes." >&2
  exit 1
fi
grep -Fq "The checkout has local changes" "$TEMP_DIR/dirty.out"
if grep -Fq " pull --ff-only" "$LOG" || grep -Fq "docker compose up" "$LOG"; then
  echo "The updater changed services despite the dirty-checkout guard." >&2
  exit 1
fi

: > "$LOG"
if TEST_ENV_TRACKED=true bash "$PROJECT_DIR/scripts/update.sh" > "$TEMP_DIR/tracked-env.out" 2>&1; then
  echo "The updater accepted a .env file tracked by Git." >&2
  exit 1
fi
grep -Fq "The .env file is tracked by Git" "$TEMP_DIR/tracked-env.out"
if grep -Fq " pull --ff-only" "$LOG" || grep -Fq "docker compose up" "$LOG"; then
  echo "The updater pulled code despite the tracked-.env guard." >&2
  exit 1
fi

: > "$LOG"
TEST_PROXY_RUNNING=true bash "$PROJECT_DIR/scripts/update.sh" > "$TEMP_DIR/proxy.out"
grep -Fq "docker compose --profile public up -d --build web scoring-worker proxy" "$LOG"

echo "Updater tests passed: backup and clean-tree guards verified; .env and volumes were preserved."
