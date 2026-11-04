#!/usr/bin/env bash
set -euo pipefail

umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f docker-compose.yml || ! -f Dockerfile || ! -f requirements.txt ]]; then
  echo "Run the installer from a complete CompetitionSystem checkout." >&2
  exit 1
fi

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

purge_docker_installation() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    mapfile -t containers < <(docker ps -aq)
    if ((${#containers[@]})); then
      docker rm -f "${containers[@]}"
    fi

    mapfile -t volumes < <(docker volume ls -q)
    if ((${#volumes[@]})); then
      docker volume rm -f "${volumes[@]}"
    fi
    docker system prune --all --volumes --force
  fi

  if command -v systemctl >/dev/null 2>&1; then
    for unit in docker.socket docker.service; do
      if systemctl is-active --quiet "$unit"; then
        systemctl stop "$unit"
      fi
    done
  fi

  docker_packages=()
  if command -v dpkg-query >/dev/null 2>&1; then
    for package in \
      docker.io \
      docker-ce \
      docker-ce-cli \
      docker-ce-rootless-extras \
      docker-buildx-plugin \
      docker-compose-plugin \
      docker-compose-v2 \
      docker-compose \
      containerd.io
    do
      if dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null |
        grep -q '^ii'; then
        docker_packages+=("$package")
      fi
    done
  fi
  if ((${#docker_packages[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${docker_packages[@]}"
  fi

  rm -rf -- /var/lib/docker /etc/docker
  hash -r
}

case "${CLEAN_DOCKER_INSTALL:-false}" in
  false) ;;
  true)
    echo "WARNING: A full Docker reset deletes every Docker container, image, network, volume, and rootful Docker data directory on this VM."
    read -r -p "Type WIPE ALL DOCKER DATA to continue: " confirmation
    if [[ "$confirmation" != "WIPE ALL DOCKER DATA" ]]; then
      echo "Docker reset cancelled; no Docker data was removed." >&2
      exit 1
    fi
    purge_docker_installation
    ;;
  *)
    echo "CLEAN_DOCKER_INSTALL must be either true or false." >&2
    exit 1
    ;;
esac

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
if ! command -v docker >/dev/null 2>&1; then
  echo "The Docker CLI is unavailable after prerequisite installation." >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL is unavailable after prerequisite installation." >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker
fi

docker_ready=false
for attempt in $(seq 1 45); do
  if docker info >/dev/null 2>&1; then
    docker_ready=true
    break
  fi
  sleep 2
done
if [[ "$docker_ready" != true ]]; then
  echo "The Docker daemon did not become ready. Check that Docker is installed and running." >&2
  exit 1
fi

validate_app_bind_address() {
  local address="$1"
  local octet
  local -a octets

  if [[ ! "$address" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    echo "APP_BIND_ADDRESS must be an IPv4 address, such as 127.0.0.1 or 192.168.1.10." >&2
    return 1
  fi

  IFS=. read -r -a octets <<< "$address"
  for octet in "${octets[@]}"; do
    if ((10#$octet > 255)); then
      echo "APP_BIND_ADDRESS must be an IPv4 address with octets from 0 to 255." >&2
      return 1
    fi
  done
}

app_bind_address="${APP_BIND_ADDRESS:-127.0.0.1}"

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
APP_BIND_ADDRESS=${app_bind_address}
SESSION_COOKIE_SECURE=false
SCORING_THREADS=64
SCORING_TICK_SECONDS=5
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
  if ! grep -q '^APP_BIND_ADDRESS=' .env; then
    printf '\nAPP_BIND_ADDRESS=127.0.0.1\n' >> .env
  fi
fi

if [[ -z "${APP_BIND_ADDRESS:-}" ]]; then
  app_bind_address="$(sed -n 's/^APP_BIND_ADDRESS=//p' .env | tail -n 1 | tr -d '\r')"
  app_bind_address="${app_bind_address:-127.0.0.1}"
fi
if [[ "${DATABASE_IMPORT_ONLY:-false}" != "true" ]]; then
  read -r -p "Web app bind IPv4 address (127.0.0.1 for local only; VM interface IP for a remote proxy) [${app_bind_address}]: " requested_app_bind_address || requested_app_bind_address=""
  if [[ -n "$requested_app_bind_address" ]]; then
    app_bind_address="$requested_app_bind_address"
  fi
fi
validate_app_bind_address "$app_bind_address"
if grep -q '^APP_BIND_ADDRESS=' .env; then
  sed -i "s/^APP_BIND_ADDRESS=.*/APP_BIND_ADDRESS=${app_bind_address}/" .env
else
  printf '\nAPP_BIND_ADDRESS=%s\n' "$app_bind_address" >> .env
fi
chmod 600 .env

credential_key="$(sed -n 's/^CHECK_CREDENTIAL_KEY=//p' .env | tail -n 1 | tr -d '\r')"
if [[ "$credential_key" == \"*\" ]]; then
  credential_key="${credential_key:1:${#credential_key}-2}"
elif [[ "$credential_key" == \'*\' ]]; then
  credential_key="${credential_key:1:${#credential_key}-2}"
fi
if [[ ! "$credential_key" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "CHECK_CREDENTIAL_KEY must be configured as 64 hexadecimal characters." >&2
  exit 1
fi

if ! docker compose config --quiet; then
  echo "Docker Compose configuration is invalid. Review the required values in .env." >&2
  exit 1
fi

if [[ "${DATABASE_IMPORT_ONLY:-false}" == "true" ]]; then
  echo "Starting PostgreSQL only for a database import."
  docker compose stop web scoring-worker >/dev/null
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

echo "Building and starting the web app on ${app_bind_address}:${APP_PORT:-8080}."
echo "This preview does not bind public ports 80/443 or replace the active site."
docker compose up -d --build --remove-orphans

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
  echo "The web app and database did not become ready. Recent service logs follow:" >&2
  docker compose logs --tail=80 web database scoring-worker >&2
  exit 1
fi

worker_ready=false
for attempt in $(seq 1 45); do
  worker_container_id="$(docker compose ps -q scoring-worker | head -n 1)"
  if [[ -n "$worker_container_id" ]] &&
    [[ "$(docker inspect --format '{{.State.Health.Status}}' "$worker_container_id" 2>/dev/null)" == "healthy" ]] &&
    docker compose exec -T scoring-worker flask --app app:create_app db-check >/dev/null 2>&1; then
    worker_ready=true
    break
  fi
  sleep 2
done
if [[ "$worker_ready" != true ]]; then
  echo "The scoring worker did not become ready. Recent service logs follow:" >&2
  docker compose logs --tail=80 web database scoring-worker >&2
  exit 1
fi

if docker compose exec -T web flask --app app:create_app has-admin >/dev/null 2>&1; then
  echo "A platform administrator already exists; leaving accounts unchanged."
else
  echo "Create the first platform administrator now."
  docker compose exec web flask --app app:create_app bootstrap-admin
fi

if ! docker compose exec -T web flask --app app:create_app has-admin >/dev/null 2>&1; then
  echo "No platform administrator was found after bootstrap." >&2
  exit 1
fi

docker compose ps

cat <<EOF

Preview is running on the VM at http://${app_bind_address}:${APP_PORT:-8080}.
EOF

if [[ "$app_bind_address" == "127.0.0.1" ]]; then
  cat <<EOF
For a private remote preview, create an SSH tunnel from your workstation:
  ssh -L ${APP_PORT:-8080}:127.0.0.1:${APP_PORT:-8080} <your-vm>
Then open http://127.0.0.1:${APP_PORT:-8080}.
EOF
else
  cat <<EOF
Configure your reverse proxy to use http://${app_bind_address}:${APP_PORT:-8080}.
Restrict this port to the proxy using your network firewall; it is plain HTTP.
EOF
fi

cat <<EOF
Once the preview is accepted and DNS/ports 80 and 443 are ready, follow the
"Public deployment and cutover" instructions in README.md.
EOF
