# Competition System

An integrated rebuild of the ScoreTrak engine, simulated banking, and competition store. The workspace began as a README-only repository; the application here is a clean implementation based on the workflows and public repository references, not a copy of their source files.

## Current platform shape

- **Flask/Python application:** one web app, rendered with server-side templates, separated into Engine, Bank, Store, and Admin areas.
- **Shared team login:** one organizer-created account per team, shared across the sites. The initial roles are administrator, competition organizer, bank operator, and team.
- **PostgreSQL:** persistent application and transaction data. New installs start empty.
- **PostgreSQL-backed scoring queue:** competition schedules and queued rounds are stored in the application database; Redis and a separate message broker are not used.
- **Containerized scoring workers:** workers poll PostgreSQL for due competitions and queued rounds. Database row locks prevent multiple workers from claiming the same round, and worker containers can be scaled with Docker Compose. Each worker processes one round at a time and runs up to `SCORING_THREADS` service probes concurrently. Supported checks include HTTP/HTTPS, DNS, ICMP PING, SSH and WinRM authentication, LDAPS search, MySQL health, and read-only FTPS. Credentials are encrypted at rest with a dedicated `CHECK_CREDENTIAL_KEY`.
- **Caddy:** optional public reverse proxy and automatic TLS for `engine`, `bank`, `store`, and `admin` subdomains.
- **Docker Compose:** the web app, scoring workers, PostgreSQL, and optional proxy run as separate containers.

Scoring workers are managed as Docker Compose services, not dynamically created per service or scoring round. The web and scoring containers do not have Docker socket access. Team challenge-container provisioning is not configured: the image names, network isolation, resource limits, and lifecycle rules are still unspecified. Once those rules are defined, challenge containers should be managed by a separately constrained Engine capability rather than granting the web container unrestricted Docker daemon access. Arbitrary custom check plugins are also not implemented yet.

## Safe preview on a clean Ubuntu VM

The setup script targets **Ubuntu Server 24.04** and checks for Docker, Docker Compose v2, and OpenSSL. It installs only missing host prerequisites. It does not install Python or application packages on the VM; those are built into the application container.

1. Clone this repository to the VM.
2. Run:

   ```bash
   sudo bash scripts/setup.sh
   ```

3. Enter the base domain when prompted. The script generates `.env` secrets, starts a **private preview on `127.0.0.1:8080`**, and prompts for the initial administrator account.
4. To view it from another machine, create an SSH tunnel:

   ```bash
   ssh -L 8080:127.0.0.1:8080 <your-vm>
   ```

   Then open `http://127.0.0.1:8080`.

The default preview deliberately avoids public ports 80/443 so it can be evaluated on the same VM without displacing the current system. `.env` contains database, signing, and check-credential encryption secrets; keep it private and back it up securely.

To migrate an existing installation, securely copy its `.env` and database backup to the VM first. Run `sudo env DATABASE_IMPORT_ONLY=true bash scripts/setup.sh` to install prerequisites and start PostgreSQL without starting the web app or scoring worker. Restore the database, then run `sudo bash scripts/setup.sh`; the existing administrator account will be detected and preserved.

## Public deployment and cutover

Before public access, make sure:

- `engine.<base-domain>`, `bank.<base-domain>`, `store.<base-domain>`, and `admin.<base-domain>` resolve to this VM.
- Inbound TCP ports 80 and 443 are available to Caddy.
- The current site has been cut over or stopped, so this stack can bind those ports.

Update `.env`:

```dotenv
COOKIE_DOMAIN=.your-base-domain
SESSION_COOKIE_SECURE=true
```

Confirm all four `*_DOMAIN` values use the intended domain, then run:

```bash
sudo docker compose --profile public up -d --build --remove-orphans
```

Caddy obtains and renews TLS certificates for the configured site hostnames. The root domain itself is not served by this stack. When upgrading an existing deployment, `--remove-orphans` stops and removes containers for services removed from Compose, including the former Redis and Celery containers; it leaves their data volumes untouched. To preview changes later, rebuild with `sudo docker compose up -d --build --remove-orphans`; to stop the stack, use `sudo docker compose down` (persistent named volumes remain).

## First use

1. Sign in at `https://admin.<base-domain>`.
2. Create team accounts and staff accounts. Team accounts are provisioned by an administrator or organizer; link a login to an existing team or create a new team. There is no public registration or email dependency.
3. Create a scoreboard through the guided builder: enter its details, create device groups and select their supported check types, configure each group's IPv4 `{index}` pattern and ending octet, enroll teams to generate one host per group, configure a score-check template for every selected check type in each group, then review the setup. The score-check step shows per-group readiness; missing, disabled, paused, or unexpected checks need attention before review.
4. Activate the competition. Scoring workers schedule rounds at the configured interval, claim queued rounds from PostgreSQL, execute checks concurrently, and record results and credit deposits. If workers are stopped, scoring pauses; after restart, workers resume from the competition's due schedule. A round already claimed by a worker that exits unexpectedly is not automatically reclaimed.
5. Add and publish store items. Team purchases use simulated credits, produce ledger transactions, and remain pending until an organizer completes or refunds the order.

The first administrator can also be created later with:

```bash
sudo docker compose exec web flask --app app:create_app bootstrap-admin
```

Password resets are performed by an administrator or organizer in the Admin area. No reset email is sent.

## Operations

```bash
sudo docker compose ps
sudo docker compose logs -f web scoring-worker
sudo docker compose logs -f proxy
```

`SCORING_THREADS` controls simultaneous service probes per scoring worker (1–64; default 8). `SCORING_TICK_SECONDS` controls how often a worker polls for due rounds (5–300 seconds; default 15). Each worker processes one scoring round at a time; scale the number of worker containers to increase round-level parallelism:

```bash
sudo docker compose up -d --scale scoring-worker=3 scoring-worker
```

Set the scale to the desired total number of worker containers; Compose starts or removes containers to match. Keep the replica count and `SCORING_THREADS` within the VM's resource limits. Competition intervals are constrained to 15–3,600 seconds. The former Redis/Celery settings (`REDIS_URL` and `CELERY_CONCURRENCY`) are no longer used and can be removed from an existing `.env`.

## Service check catalog

The supplied legacy exports store check settings as service properties. This implementation keeps each supported service's typed settings in its own `check_config` object and records a `check_type`; it does not import the exports or their credentials.

| Legacy check | Properties seen in the exports | Current status |
|---|---|---|
| HTTP/HTTPS | `Scheme`, `Subdomain`, `Path`, `Port`, `ExpectedOutput` | Supported; checks status and optional response text |
| DNS | `Lookup`, `ExpectedOutput` | Supported; A, AAAA, CNAME, MX, and TXT answers with a bounded resolver timeout |
| PING | `Protocol`, `Attempts` | Supported; 1–4 ICMP echo requests, with a bounded process timeout |
| SSH | `Port`, `Username`, `Password`, optional host-key verification | Authentication-only; host-key pinning is recommended, but can be explicitly skipped with a man-in-the-middle credential risk warning. Never opens a shell |
| WinRM | `Scheme`, `ClientType`, `Port`, `Username`, `Password`, `Command` | Supported for HTTPS authentication only; opens and closes a temporary shell without running `Command` |
| LDAP | `ApplicationProtocol`, `TransportProtocol`, `Domain`, `BaseDN`, `Filter`, `Attributes`, `Port`, `Username`, `Password` | Supported as bounded, read-only LDAPS search with certificate validation |
| SQL | `DBType`, `DBName`, `Port`, `Username`, `Password`, `Command`, `MinExpectedRows`, `MaxExpectedRows` | Supported for MySQL TLS authentication using fixed `SELECT 1`; user SQL is never executed |
| FTP | `Port`, `Username`, `Password`, `ReadFilename`, `WriteFilename`, `Text`, `ExpectedOutput` | Supported as read-only FTPS over verified TLS; remote writes are not supported |

Service scoring metadata maps `weight` to base points and `pointsBoost` to a flat bonus added when a check passes. A paused service is skipped and earns no points; hiding a service does not remove its points from team totals. The public scoreboard keeps the standard Competition System page layout and displays a compact grid with solid pass/fail cells. It updates in place using ETag-conditional polling, without a full-page refresh. Its active-competition selector is hidden by default and can be enabled from Admin → Management controls. `roundUnits` and `roundDelay` are not implemented because their scheduling behavior still needs to be specified. The files include password and password-hash fields. Do not commit or import them, and rotate their credentials if they are still valid.

Usernames and passwords for SSH, WinRM, LDAPS, MySQL, and FTPS are encrypted before being saved in the service configuration. The setup script generates a dedicated `CHECK_CREDENTIAL_KEY`; back up and protect it separately from `SECRET_KEY`. Do not change or lose it without re-entering affected check credentials. Existing credentials encrypted by an earlier release can still be decrypted with the unchanged `SECRET_KEY`; re-save those checks after upgrading to encrypt them with the dedicated key. SSH checks pin the configured SHA256 host-key fingerprint by default; administrators can explicitly skip verification, which makes the check vulnerable to man-in-the-middle servers capturing credentials. WinRM requires HTTPS and certificate validation; supported authentication transports are NTLM and Basic (over HTTPS). TLS checks validate certificates; configure trusted certificates on the Docker image/host as appropriate. Arbitrary WinRM/SSH commands, user-supplied SQL statements, and FTP writes are deliberately not run.

The current Engine UI separates service metadata from check properties. This platform maps `Weight`, `Points Boost`, pause/hide flags, and service-group membership. Service groups are organizational metadata only; they do not deploy workers. The scoreboard builder creates device groups before IP templates; each group selects supported check types, then receives its own IPv4 `{index}` pattern, ending octet, and team index range. Every device group gets one generated host address per enrolled team. The score-check step opens a configuration form for every selected group/check-type combination that does not yet have an enabled template; the same template runs for every enrolled team's matching host. Team indexes are assigned in ascending team-ID order, and generated addresses can be edited per team. Admins can optionally allow a team to edit only its own host address, restricted to configured CIDR ranges or exact hostnames. From the team-check page, a team can update only its own unlocked username/password fields; credential overrides are encrypted at rest and used only when scoring that team's host. Organizers and admins can select any active competition team, while banker accounts are denied access. Changing a group's template regenerates addresses for its hosts. DNS check forms can fill the expected answer from the current selected DNS record; if left blank on save, the form resolves the answer server-side with a bounded timeout. SMB, IMAP, and CalDav are shown in the device-group type list for reference but are not currently implemented. Per-service `Round Units`/`Round Delay` still need explicit scheduling requirements. By default SQL uses fixed `SELECT 1` and WinRM/SSH check authentication only.

### Opt-in legacy compatibility modes

Each SSH, WinRM, LDAP, SQL, FTP, and DNS check form has a collapsed **Legacy compatibility** box (`legacy_mode`) that reproduces the old Engine behavior so a scoreboard can match it exactly: SSH/WinRM run a configured command (for example `whoami`) and require the expected text in its output; WinRM can use plain `http`; LDAP can use plain LDAP with `user@domain`; MySQL can run one read-only `SELECT` (no `;`) and check a min/max row count without TLS; FTP can use plain FTP and write a file before reading it; DNS can query the target host directly and default the expected answer to `{address}` (the host's own IP). These modes send credentials without TLS and execute administrator-configured commands, so enable them only on isolated competition networks.

### Mirroring the live Engine scoreboard

A mirror of the live Engine scoreboard ("UBNetDef Engine Mirror": 15 teams, 7 device groups, 105 hosts as `10.1.{team}.{octet}`, 14 check templates) is active. Scoring requires network access to those hosts. Team accounts and credentials were not copied from the live Engine, and where live per-team passwords differed (Dev Server SQL, WebApp FTP), the majority password was used.

To edit a configured check, use **Edit** in the competition's service-check table. Its type is fixed, while supported settings, points, timeout, boost, pause/hide status, and group assignment can be updated. Credential fields stay blank; leave both blank to retain the encrypted credentials, or enter both to replace them. Legacy TCP checks can be edited but cannot be newly created.

## Development

The app's dependencies are in `requirements.txt`; run it locally with a virtual environment and a local PostgreSQL service, or use Compose. Compose requires a `.env` file containing the database password and app secrets. For a local setup, copy `.env.example` to `.env`, replace the placeholder values for `SECRET_KEY`, `CHECK_CREDENTIAL_KEY`, and `POSTGRES_PASSWORD` with unique secret values, and adjust the domains if needed. The setup script generates these values automatically on a clean Ubuntu VM. The Compose preview binds only to localhost:

```bash
docker compose up --build
```

The first run applies database migrations. Create an administrator with `docker compose exec web flask --app app:create_app bootstrap-admin`. No test data or default users are seeded. Redis is not required. When upgrading from a previous Compose setup, use `docker compose up --build --remove-orphans` so removed Redis/Celery containers are also stopped and removed; this does not delete their volumes.
