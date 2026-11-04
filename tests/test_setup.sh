#!/usr/bin/env bash
set -euo pipefail
trap 'status=$?; printf "Installer test failed (status %s): %s\n" "$status" "$BASH_COMMAND" >&2' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer test as root inside an Ubuntu 24.04 container." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "This installer test requires Ubuntu 24.04; found ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT

make_project() {
  local project_dir="$1"
  mkdir -p "$project_dir/scripts"
  cp "$ROOT_DIR/scripts/setup.sh" "$project_dir/scripts/setup.sh"
  cp "$ROOT_DIR/docker-compose.yml" "$project_dir/docker-compose.yml"
  cp "$ROOT_DIR/Dockerfile" "$project_dir/Dockerfile"
  cp "$ROOT_DIR/requirements.txt" "$project_dir/requirements.txt"
}

make_stubs() {
  local stub_dir="$1"
  mkdir -p "$stub_dir"

  cat > "$stub_dir/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$TEST_DOCKER_LOG"
if [[ "$1" == "info" ]]; then
  exit 0
fi
if [[ "$1" == "ps" && "${2:-}" == "-aq" ]]; then
  echo "test-container"
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  echo "healthy"
  exit 0
fi
if [[ "$1" == "volume" && "$2" == "ls" && "${3:-}" == "-q" ]]; then
  echo "test-volume"
  exit 0
fi
if [[ "$1" == "compose" && "$2" == "version" ]]; then
  echo "Docker Compose version v2.test"
  exit 0
fi
if [[ "$1" == "compose" && "$2" == "ps" && "${3:-}" == "-q" ]]; then
  echo "worker-id"
  exit 0
fi
case " $* " in
  *" has-admin "*)
    [[ -f "$TEST_ADMIN_EXISTS" ]]
    ;;
  *" bootstrap-admin "*)
    touch "$TEST_ADMIN_EXISTS"
    ;;
esac
STUB
  chmod 700 "$stub_dir/docker"

  cat > "$stub_dir/openssl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
count=0
if [[ -f "$TEST_OPENSSL_COUNTER" ]]; then
  read -r count < "$TEST_OPENSSL_COUNTER"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$TEST_OPENSSL_COUNTER"
length=$(("${3:-32}" * 2))
character=a
if [[ "$count" == "2" ]]; then
  character=b
elif [[ "$count" == "3" ]]; then
  character=c
fi
printf '%*s' "$length" '' | tr ' ' "$character"
STUB
  chmod 700 "$stub_dir/openssl"

  cat > "$stub_dir/systemctl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
exit 0
STUB
  chmod 700 "$stub_dir/systemctl"
}

make_project "$TEMP_DIR/fresh"
make_stubs "$TEMP_DIR/fresh-stubs"
FRESH_LOG="$TEMP_DIR/fresh-docker.log"
FRESH_ADMIN="$TEMP_DIR/fresh-admin"
FRESH_COUNTER="$TEMP_DIR/fresh-openssl-count"
FRESH_OUTPUT="$TEMP_DIR/fresh-output.log"
TEST_PATH="$TEMP_DIR/fresh-stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if ! (
  cd "$TEMP_DIR/fresh"
  printf '%s\n' "competition.example.org" |
    env PATH="$TEST_PATH" \
      TEST_DOCKER_LOG="$FRESH_LOG" \
      TEST_ADMIN_EXISTS="$FRESH_ADMIN" \
      TEST_OPENSSL_COUNTER="$FRESH_COUNTER" \
      bash scripts/setup.sh
) > "$FRESH_OUTPUT" 2>&1; then
  cat "$FRESH_OUTPUT" >&2
  exit 1
fi

generated_secret="$(sed -n 's/^SECRET_KEY=//p' "$TEMP_DIR/fresh/.env")"
generated_credential_key="$(sed -n 's/^CHECK_CREDENTIAL_KEY=//p' "$TEMP_DIR/fresh/.env")"
generated_database_password="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$TEMP_DIR/fresh/.env")"
[[ "$generated_secret" =~ ^[a-f]{64}$ ]]
[[ "$generated_credential_key" =~ ^[a-f]{64}$ ]]
[[ "$generated_database_password" =~ ^[a-f]{48}$ ]]
[[ "$generated_secret" != "$generated_credential_key" ]]
[[ "$(stat -c '%a' "$TEMP_DIR/fresh/.env")" == "600" ]]
grep -Fxq "BASE_DOMAIN=competition.example.org" "$TEMP_DIR/fresh/.env"
grep -Fxq "APP_BIND_ADDRESS=127.0.0.1" "$TEMP_DIR/fresh/.env"
grep -Fxq "SCORING_THREADS=64" "$TEMP_DIR/fresh/.env"
grep -Fxq "SCORING_TICK_SECONDS=5" "$TEMP_DIR/fresh/.env"
grep -Fq "compose config --quiet" "$FRESH_LOG"
grep -Fq "compose up -d --build --remove-orphans" "$FRESH_LOG"
grep -Fq "compose exec -T web python -c" "$FRESH_LOG"
grep -Fq "compose exec -T scoring-worker flask --app app:create_app db-check" "$FRESH_LOG"
grep -Fq "compose exec web flask --app app:create_app bootstrap-admin" "$FRESH_LOG"
[[ -f "$FRESH_ADMIN" ]]
if grep -Fq "rm -f test-container" "$FRESH_LOG"; then
  echo "Default installer mode unexpectedly entered the Docker reset path." >&2
  exit 1
fi

make_project "$TEMP_DIR/proxy-bind"
make_stubs "$TEMP_DIR/proxy-bind-stubs"
PROXY_BIND_PATH="$TEMP_DIR/proxy-bind-stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROXY_BIND_ADMIN="$TEMP_DIR/proxy-bind-admin"
PROXY_BIND_COUNTER="$TEMP_DIR/proxy-bind-openssl-count"
PROXY_BIND_INITIAL_LOG="$TEMP_DIR/proxy-bind-initial-docker.log"
PROXY_BIND_INITIAL_OUTPUT="$TEMP_DIR/proxy-bind-initial-output.log"
if ! (
  cd "$TEMP_DIR/proxy-bind"
  printf '%s\n' "competition.example.org" |
    env PATH="$PROXY_BIND_PATH" \
      TEST_DOCKER_LOG="$PROXY_BIND_INITIAL_LOG" \
      TEST_ADMIN_EXISTS="$PROXY_BIND_ADMIN" \
      TEST_OPENSSL_COUNTER="$PROXY_BIND_COUNTER" \
      bash scripts/setup.sh
) > "$PROXY_BIND_INITIAL_OUTPUT" 2>&1; then
  cat "$PROXY_BIND_INITIAL_OUTPUT" >&2
  exit 1
fi

PROXY_BIND_LOG="$TEMP_DIR/proxy-bind-docker.log"
PROXY_BIND_OUTPUT="$TEMP_DIR/proxy-bind-output.log"
if ! (
  cd "$TEMP_DIR/proxy-bind"
  printf '%s\n' "192.0.2.10" |
    env PATH="$PROXY_BIND_PATH" \
      TEST_DOCKER_LOG="$PROXY_BIND_LOG" \
      TEST_ADMIN_EXISTS="$PROXY_BIND_ADMIN" \
      TEST_OPENSSL_COUNTER="$PROXY_BIND_COUNTER" \
      bash scripts/setup.sh
) > "$PROXY_BIND_OUTPUT" 2>&1; then
  cat "$PROXY_BIND_OUTPUT" >&2
  exit 1
fi
grep -Fxq "APP_BIND_ADDRESS=192.0.2.10" "$TEMP_DIR/proxy-bind/.env"
grep -Fq "http://192.0.2.10:8080" "$PROXY_BIND_OUTPUT"
grep -Fq "compose up -d --build --remove-orphans" "$PROXY_BIND_LOG"

sed -i 's/^APP_BIND_ADDRESS=.*/APP_BIND_ADDRESS=999.2.3.4/' "$TEMP_DIR/proxy-bind/.env"
INVALID_BIND_LOG="$TEMP_DIR/invalid-bind-docker.log"
INVALID_BIND_OUTPUT="$TEMP_DIR/invalid-bind-output.log"
if (
  cd "$TEMP_DIR/proxy-bind"
  env PATH="$PROXY_BIND_PATH" \
    TEST_DOCKER_LOG="$INVALID_BIND_LOG" \
    TEST_ADMIN_EXISTS="$PROXY_BIND_ADMIN" \
    TEST_OPENSSL_COUNTER="$PROXY_BIND_COUNTER" \
    bash scripts/setup.sh
) > "$INVALID_BIND_OUTPUT" 2>&1; then
  echo "Installer accepted an invalid APP_BIND_ADDRESS." >&2
  exit 1
fi
grep -Fq "APP_BIND_ADDRESS must be an IPv4 address with octets from 0 to 255." "$INVALID_BIND_OUTPUT"
if grep -Fq "compose up -d --build" "$INVALID_BIND_LOG"; then
  echo "Installer deployed with an invalid APP_BIND_ADDRESS." >&2
  exit 1
fi

PRESERVED_SECRET="$generated_secret"
RERUN_LOG="$TEMP_DIR/rerun-docker.log"
RERUN_OUTPUT="$TEMP_DIR/rerun-output.log"
if ! (
  cd "$TEMP_DIR/fresh"
  env PATH="$TEST_PATH" \
    TEST_DOCKER_LOG="$RERUN_LOG" \
    TEST_ADMIN_EXISTS="$FRESH_ADMIN" \
    TEST_OPENSSL_COUNTER="$FRESH_COUNTER" \
    bash scripts/setup.sh
) > "$RERUN_OUTPUT" 2>&1; then
  cat "$RERUN_OUTPUT" >&2
  exit 1
fi
[[ "$(sed -n 's/^SECRET_KEY=//p' "$TEMP_DIR/fresh/.env")" == "$PRESERVED_SECRET" ]]
grep -Fq "compose exec -T web flask --app app:create_app has-admin" "$RERUN_LOG"
if grep -Fq "bootstrap-admin" "$RERUN_LOG"; then
  echo "Installer attempted to replace an existing administrator." >&2
  exit 1
fi

IMPORT_LOG="$TEMP_DIR/import-docker.log"
IMPORT_OUTPUT="$TEMP_DIR/import-output.log"
if ! (
  cd "$TEMP_DIR/fresh"
  env PATH="$TEST_PATH" \
    TEST_DOCKER_LOG="$IMPORT_LOG" \
    TEST_ADMIN_EXISTS="$FRESH_ADMIN" \
    TEST_OPENSSL_COUNTER="$FRESH_COUNTER" \
    DATABASE_IMPORT_ONLY=true \
    bash scripts/setup.sh
) > "$IMPORT_OUTPUT" 2>&1; then
  cat "$IMPORT_OUTPUT" >&2
  exit 1
fi
grep -Fq "compose stop web scoring-worker" "$IMPORT_LOG"
grep -Fq "compose up -d database" "$IMPORT_LOG"
if grep -Fq "compose up -d --build" "$IMPORT_LOG"; then
  echo "Database-import mode unexpectedly started the application." >&2
  exit 1
fi

sed -i 's/^CHECK_CREDENTIAL_KEY=.*/CHECK_CREDENTIAL_KEY=invalid/' "$TEMP_DIR/fresh/.env"
INVALID_LOG="$TEMP_DIR/invalid-docker.log"
INVALID_OUTPUT="$TEMP_DIR/invalid-output.log"
if (
  cd "$TEMP_DIR/fresh"
  env PATH="$TEST_PATH" \
    TEST_DOCKER_LOG="$INVALID_LOG" \
    TEST_ADMIN_EXISTS="$FRESH_ADMIN" \
    TEST_OPENSSL_COUNTER="$FRESH_COUNTER" \
    bash scripts/setup.sh
) > "$INVALID_OUTPUT" 2>&1; then
  echo "Installer accepted an invalid CHECK_CREDENTIAL_KEY." >&2
  exit 1
fi
grep -Fq "CHECK_CREDENTIAL_KEY must be configured as 64 hexadecimal characters." "$INVALID_OUTPUT"
if grep -Fq "compose up -d --build" "$INVALID_LOG"; then
  echo "Installer deployed with an invalid credential-encryption key." >&2
  exit 1
fi

make_project "$TEMP_DIR/missing-docker"
make_stubs "$TEMP_DIR/missing-docker-stubs"
MISSING_DOCKER_LOG="$TEMP_DIR/missing-docker.log"
MISSING_DOCKER_ADMIN="$TEMP_DIR/missing-docker-admin"
MISSING_DOCKER_COUNTER="$TEMP_DIR/missing-docker-openssl-count"
MISSING_DOCKER_OUTPUT="$TEMP_DIR/missing-docker-output.log"
cat > "$TEMP_DIR/missing-docker-stubs/apt-get" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$TEST_APT_LOG"
if [[ "$1" == "install" ]]; then
  cp "$TEST_DOCKER_TEMPLATE" "$TEST_STUB_BIN/docker"
  chmod 700 "$TEST_STUB_BIN/docker"
  cp "$TEST_OPENSSL_TEMPLATE" "$TEST_STUB_BIN/openssl"
  chmod 700 "$TEST_STUB_BIN/openssl"
fi
STUB
chmod 700 "$TEMP_DIR/missing-docker-stubs/apt-get"
rm "$TEMP_DIR/missing-docker-stubs/docker"
rm "$TEMP_DIR/missing-docker-stubs/openssl"
MISSING_DOCKER_PATH="$TEMP_DIR/missing-docker-stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if ! (
  cd "$TEMP_DIR/missing-docker"
  printf '%s\n' "competition.example.org" |
    env PATH="$MISSING_DOCKER_PATH" \
      TEST_DOCKER_LOG="$MISSING_DOCKER_LOG" \
      TEST_ADMIN_EXISTS="$MISSING_DOCKER_ADMIN" \
      TEST_OPENSSL_COUNTER="$MISSING_DOCKER_COUNTER" \
      TEST_APT_LOG="$TEMP_DIR/apt.log" \
      TEST_DOCKER_TEMPLATE="$TEMP_DIR/fresh-stubs/docker" \
      TEST_OPENSSL_TEMPLATE="$TEMP_DIR/fresh-stubs/openssl" \
      TEST_STUB_BIN="$TEMP_DIR/missing-docker-stubs" \
      bash scripts/setup.sh
) > "$MISSING_DOCKER_OUTPUT" 2>&1; then
  cat "$MISSING_DOCKER_OUTPUT" >&2
  exit 1
fi
grep -Fq "update" "$TEMP_DIR/apt.log"
grep -Fq "docker.io" "$TEMP_DIR/apt.log"
grep -Fq "docker-compose-v2" "$TEMP_DIR/apt.log"
grep -Fq "openssl" "$TEMP_DIR/apt.log"
grep -Fq "compose up -d --build --remove-orphans" "$MISSING_DOCKER_LOG"

make_project "$TEMP_DIR/cancel-reset"
make_stubs "$TEMP_DIR/cancel-reset-stubs"
CANCEL_RESET_OUTPUT="$TEMP_DIR/cancel-reset-output.log"
if (
  cd "$TEMP_DIR/cancel-reset"
  printf '%s\n' "NO" |
    env PATH="$TEMP_DIR/cancel-reset-stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      CLEAN_DOCKER_INSTALL=true \
      TEST_DOCKER_LOG="$TEMP_DIR/cancel-reset-docker.log" \
      TEST_ADMIN_EXISTS="$TEMP_DIR/cancel-reset-admin" \
      TEST_OPENSSL_COUNTER="$TEMP_DIR/cancel-reset-openssl-count" \
      bash scripts/setup.sh
) > "$CANCEL_RESET_OUTPUT" 2>&1; then
  echo "Expected an incorrect reset confirmation to cancel installation." >&2
  exit 1
fi
grep -Fq "Docker reset cancelled" "$CANCEL_RESET_OUTPUT"
[[ ! -e "$TEMP_DIR/cancel-reset-docker.log" ]]
[[ ! -e "$TEMP_DIR/cancel-reset-admin" ]]

make_project "$TEMP_DIR/full-reset"
make_stubs "$TEMP_DIR/full-reset-stubs"
FULL_RESET_LOG="$TEMP_DIR/full-reset-docker.log"
FULL_RESET_APT_LOG="$TEMP_DIR/full-reset-apt.log"
FULL_RESET_ADMIN="$TEMP_DIR/full-reset-admin"
FULL_RESET_COUNTER="$TEMP_DIR/full-reset-openssl-count"
FULL_RESET_OUTPUT="$TEMP_DIR/full-reset-output.log"
cat > "$TEMP_DIR/full-reset-stubs/dpkg-query" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
echo "ii"
STUB
chmod 700 "$TEMP_DIR/full-reset-stubs/dpkg-query"
cat > "$TEMP_DIR/full-reset-stubs/apt-get" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$TEST_APT_LOG"
if [[ "$1" == "purge" ]]; then
  rm -f "$TEST_STUB_BIN/docker"
elif [[ "$1" == "install" ]]; then
  cp "$TEST_DOCKER_TEMPLATE" "$TEST_STUB_BIN/docker"
  chmod 700 "$TEST_STUB_BIN/docker"
fi
STUB
chmod 700 "$TEMP_DIR/full-reset-stubs/apt-get"
FULL_RESET_PATH="$TEMP_DIR/full-reset-stubs:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if ! (
  cd "$TEMP_DIR/full-reset"
  printf '%s\n' "WIPE ALL DOCKER DATA" "competition.example.org" |
    env PATH="$FULL_RESET_PATH" \
      CLEAN_DOCKER_INSTALL=true \
      TEST_DOCKER_LOG="$FULL_RESET_LOG" \
      TEST_ADMIN_EXISTS="$FULL_RESET_ADMIN" \
      TEST_OPENSSL_COUNTER="$FULL_RESET_COUNTER" \
      TEST_APT_LOG="$FULL_RESET_APT_LOG" \
      TEST_DOCKER_TEMPLATE="$TEMP_DIR/fresh-stubs/docker" \
      TEST_STUB_BIN="$TEMP_DIR/full-reset-stubs" \
      bash scripts/setup.sh
) > "$FULL_RESET_OUTPUT" 2>&1; then
  cat "$FULL_RESET_OUTPUT" >&2
  exit 1
fi
grep -Fq "rm -f test-container" "$FULL_RESET_LOG"
grep -Fq "volume rm -f test-volume" "$FULL_RESET_LOG"
grep -Fq "system prune --all --volumes --force" "$FULL_RESET_LOG"
grep -Fq "purge -y docker.io" "$FULL_RESET_APT_LOG"
if ! grep -Fq "install -y docker.io docker-compose-v2" "$FULL_RESET_APT_LOG"; then
  cat "$FULL_RESET_APT_LOG" >&2
  cat "$FULL_RESET_OUTPUT" >&2
  exit 1
fi
grep -Fq "A full Docker reset deletes every Docker container" "$FULL_RESET_OUTPUT"

echo "Installer flow tests passed."
