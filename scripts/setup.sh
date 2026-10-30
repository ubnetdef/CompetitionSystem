#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this setup with sudo: sudo bash scripts/setup.sh" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify this operating system (/etc/os-release is missing)." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "This installer targets Ubuntu Server 24.04; found ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

install_packages=()
if ! command -v docker >/dev/null 2>&1; then
  install_packages+=(docker.io)
fi
if ! docker compose version >/dev/null 2>&1; then
  install_packages+=(docker-compose-v2)
fi
if ! command -v openssl >/dev/null 2>&1; then
  install_packages+=(openssl)
fi

if ((${#install_packages[@]})); then
  echo "Installing missing host prerequisites: ${install_packages[*]}"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${install_packages[@]}"
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is unavailable after prerequisite installation." >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker
fi

if [[ ! -f .env ]]; then
  read -r -p "Base domain for engine/bank/store/admin sites (example: competition.example.org): " base_domain
  if [[ ! "$base_domain" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; then
    echo "Enter a hostname only (no scheme, port, slash, or spaces)." >&2
    exit 1
  fi

  secret_key="$(openssl rand -hex 32)"
  check_credential_key="$(openssl rand -hex 32)"
  postgres_password="$(openssl rand -hex 24)"
  cat > .env <<EOF
BASE_DOMAIN=${base_domain}
ENGINE_DOMAIN=engine.${base_domain}
BANK_DOMAIN=bank.${base_domain}
STORE_DOMAIN=store.${base_domain}
ADMIN_DOMAIN=admin.${base_domain}
COOKIE_DOMAIN=
SECRET_KEY=${secret_key}
CHECK_CREDENTIAL_KEY=${check_credential_key}
POSTGRES_PASSWORD=${postgres_password}
HTTP_PORT=80
HTTPS_PORT=443
APP_PORT=8080
SESSION_COOKIE_SECURE=false
SCORING_THREADS=8
SCORING_TICK_SECONDS=15
DEFAULT_PAYOUT_PER_POINT=10
EOF
  chmod 600 .env
  echo "Created .env with generated secrets. Keep this file private and backed up."
else
  echo "Keeping the existing .env configuration."
  if ! grep -q '^CHECK_CREDENTIAL_KEY=' .env; then
    printf '\nCHECK_CREDENTIAL_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
    chmod 600 .env
    echo "Added a new credential-encryption key to .env. Keep it private and backed up."
  fi
fi

if [[ "${DATABASE_IMPORT_ONLY:-false}" == "true" ]]; then
  echo "Starting PostgreSQL only for a database import."
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
    echo "The database did not become ready. Recent database logs follow:" >&2
    docker compose logs --tail=80 database >&2
    exit 1
  fi

  echo "The database is ready for import; the web app and scoring worker remain stopped."
  exit 0
fi

echo "Building and starting the private preview on 127.0.0.1:${APP_PORT:-8080}."
echo "This preview does not bind public ports 80/443 or replace the active site."
docker compose up -d --build --remove-orphans

ready=false
for attempt in $(seq 1 45); do
  if docker compose exec -T web flask --app app:create_app db-check >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 2
done

if [[ "$ready" != true ]]; then
  echo "The application did not become ready. Recent service logs follow:" >&2
  docker compose logs --tail=80 web database scoring-worker >&2
  exit 1
fi

if docker compose exec -T web flask --app app:create_app has-admin >/dev/null 2>&1; then
  echo "A platform administrator already exists; leaving accounts unchanged."
else
  echo "Create the first platform administrator now."
  docker compose exec web flask --app app:create_app bootstrap-admin
fi

cat <<EOF

Preview is running on the VM at http://127.0.0.1:${APP_PORT:-8080}.
For a private remote preview, create an SSH tunnel from your workstation:
  ssh -L ${APP_PORT:-8080}:127.0.0.1:${APP_PORT:-8080} <your-vm>
Then open http://127.0.0.1:${APP_PORT:-8080}.

Once the preview is accepted and DNS/ports 80 and 443 are ready, follow the
"Public deployment and cutover" instructions in README.md.
EOF
