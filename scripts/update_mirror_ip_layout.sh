#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this update with sudo: sudo bash scripts/update_mirror_ip_layout.sh" >&2
  exit 1
fi

BACKUP_DIR="/var/backups/competition-ip-layout"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077
DATABASE_BACKUP="$(mktemp "$BACKUP_DIR/before-update.XXXXXX")"

echo "Stopping the scoring worker before changing host addresses."
docker compose stop scoring-worker

echo "Backing up the database to $DATABASE_BACKUP."
docker compose exec -T database pg_dump -U competition -d competition -Fc > "$DATABASE_BACKUP"
if [[ ! -s "$DATABASE_BACKUP" ]]; then
  echo "The database backup is empty; refusing to change host addresses." >&2
  exit 1
fi

echo "Updating the active mirror from 10.1.{team}.{octet} to 10.{team}.1.{octet}."
docker compose exec -T database psql \
  -U competition -d competition -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
DO $ip_layout$
DECLARE
  target_competition_id INTEGER;
  host_total INTEGER;
  template_total INTEGER;
BEGIN
  SELECT id INTO target_competition_id
  FROM competitions
  WHERE name = 'UBNetDef Engine Mirror' AND status = 'active'
  ORDER BY id DESC
  LIMIT 1;

  IF target_competition_id IS NULL THEN
    RAISE EXCEPTION 'Active UBNetDef Engine Mirror competition was not found.';
  END IF;

  SELECT count(*) INTO host_total
  FROM hosts
  WHERE competition_id = target_competition_id;
  IF host_total <> 105 THEN
    RAISE EXCEPTION 'Expected 105 mirror hosts; found %.', host_total;
  END IF;

  SELECT count(*) INTO template_total
  FROM ip_templates
  WHERE competition_id = target_competition_id;
  IF template_total <> 7 THEN
    RAISE EXCEPTION 'Expected 7 mirror IP templates; found %.', template_total;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM hosts
    WHERE competition_id = target_competition_id
      AND address !~ '^10\.1\.[0-9]{1,3}\.[0-9]{1,3}$'
      AND address !~ '^10\.[0-9]{1,3}\.1\.[0-9]{1,3}$'
  ) THEN
    RAISE EXCEPTION 'Unexpected custom host address found; no addresses were changed.';
  END IF;

  UPDATE ip_templates
  SET address_template = replace(
    address_template,
    '10.1.{index}.',
    '10.{index}.1.'
  )
  WHERE competition_id = target_competition_id
    AND address_template LIKE '10.1.{index}.%';

  UPDATE hosts
  SET address = '10.' || split_part(address, '.', 3) || '.1.' ||
    split_part(address, '.', 4)
  WHERE competition_id = target_competition_id
    AND address ~ '^10\.1\.[0-9]{1,3}\.[0-9]{1,3}$';

  IF EXISTS (
    SELECT 1
    FROM hosts
    WHERE competition_id = target_competition_id
      AND address !~ '^10\.[0-9]{1,3}\.1\.[0-9]{1,3}$'
  ) THEN
    RAISE EXCEPTION 'Host address post-check failed; rolling back.';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM ip_templates
    WHERE competition_id = target_competition_id
      AND address_template NOT LIKE '10.{index}.1.%'
  ) THEN
    RAISE EXCEPTION 'IP template post-check failed; rolling back.';
  END IF;
END;
$ip_layout$;
COMMIT;
SQL

echo "Restarting the scoring worker."
docker compose up -d scoring-worker
docker compose ps scoring-worker
echo "Address update complete. Database backup: $DATABASE_BACKUP"
