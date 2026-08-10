# Homelab Vault

A fast, searchable, health-aware launcher for the lab.

```text
Find a service → see if it's alive → open it
```

Nothing else. If a feature does not serve that sentence, it is not here.

## Stack

| | |
|---|---|
| Backend | Django + Django Ninja |
| Frontend | Vue 3 SPA (Vite), served by Django via whitenoise |
| Database | PostgreSQL — in production and in development alike |
| Workers | `health_worker` (health checks, asyncio + httpx) and `proxmox_worker` (optional location enrichment) — separate processes, so one being slow never delays the other |
| Packaging | `docker compose` — four services: `web`, `worker`, `proxmox-worker`, `db` |

```text
backend/    Django project (config/, services/)
frontend/   Vue 3 + Vite SPA
ops/        backup.sh (nightly dump), watchdog.sh (external liveness check)
```

## Quick start (Docker)

```bash
cp .env.example .env
# Fill in the three required values at the top — SECRET_KEY,
# POSTGRES_PASSWORD, HOMELABVAULT_ADMIN_TOKEN. Generate each with:
#   openssl rand -hex 32

# The recovery export is written to a BIND-mounted host directory, and the
# containers run as uid 10001 — an unprepared directory means every export
# fails silently. Create it once before first start:
mkdir -p _export && chown 10001:10001 _export

docker compose up --build
```

Everything else in `.env.example` is commented out and already has a working
default; uncomment a line only to change it. Compose refuses to start if one of
the three required values is missing, naming the one it wants — there are no
default secrets to forget to change.

Then open <http://localhost:8000>. A fresh install lands on the empty state; add
your first service from there — no database access and no source reading needed.

**This is the complete default experience.** Proxmox is an optional
enrichment (it annotates each service with the node and guest it runs on) and
nothing else changes without it: with `PROXMOX_HOST` unset there is no polling,
no Proxmox panel, and no degradation anywhere else. The Tailscale CGNAT range
in the probe allowlist is likewise just a default — nothing requires a tailnet.

## Local development (no Docker)

Python 3.13+ and Node 22+ (what the container images pin).

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r backend/requirements-dev.txt

cd backend
python manage.py migrate
python manage.py runserver              # http://localhost:8000
python manage.py health_worker          # in a second terminal
python manage.py proxmox_worker         # optional, only with a provider configured
```

Frontend:

```bash
cd frontend
npm ci
npm run build     # Django serves the build output from frontend/dist
npm run dev       # or: Vite dev server on :5173, proxying /api to :8000
```

Tests:

```bash
cd backend && python -m pytest      # pytest + pytest-django, needs PostgreSQL
cd frontend && npm test             # vitest
```

`DATABASE_URL` is required and there is no SQLite fallback, so the backend
suite needs a reachable PostgreSQL server — there is no offline test run. The
role needs `LOGIN` and `CREATEDB`: pytest-django creates and drops
`test_<dbname>` on every run, and without `CREATEDB` the suite dies at setup
rather than at a test.

## Configuration

Deploy-time configuration lives in environment variables. Runtime preferences —
landing page, panel visibility, health thresholds, notification and Proxmox
toggles — live in the **Settings screen** (admin session required), which also
shows the env-owned controls read-only, each labelled with its variable name.

Two mechanisms are at work in `docker-compose.yml`: `${VAR}` is substituted by
Compose *from `.env`*, while the `environment:` block is what actually reaches
the container. A variable must appear in both to have any effect — which is why
`.env.example` and the compose file are kept in step.

### Required

| Variable | Meaning |
|---|---|
| `SECRET_KEY` | Django secret. The insecure dev default is refused once `DEBUG=False`. |
| `POSTGRES_PASSWORD` | Database password. No default. |
| `HOMELABVAULT_ADMIN_TOKEN` | Full access including `DELETE` and `/api/v1/probe`. The UI exchanges this for a session cookie. |

### Access

| Variable | Meaning | Default |
|---|---|---|
| `HOMELABVAULT_AGENT_TOKEN` | `GET`, `POST`, `PATCH` only, for scripts and agents. | unset — no agent access |
| `HOMELABVAULT_AUTH_MODE` | `full` (token for every write), `open-writes` (no token to add/edit; the four `DELETE`s still need admin), or `open`. Fails closed: unset or misspelled is `full`. Probe, icon and settings endpoints stay admin-only in every mode. | `full` |
| `ADMIN_REAUTH_WINDOW_S` | Destructive `DELETE`s re-prompt for the admin token after this many seconds. | `300` |
| `SESSION_LOGIN_MAX_ATTEMPTS` / `SESSION_LOGIN_WINDOW_S` | Failed admin-token attempts per IP before a lockout window. | `10` / `300` |
| `SESSION_COOKIE_AGE` | Admin session lifetime, a fixed window from login (never sliding). | `1209600` (14 days) |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | Compose forces these **off** by default: over plain http the browser silently drops Secure cookies and every UI write fails looking like a CSRF bug. Set `True` once behind https. | `False` |
| `SECURE_HSTS_SECONDS` / `SECURE_SSL_REDIRECT` | Standard Django TLS hardening, off for plain-http LAN use. | `0` / `False` |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | Set the second when serving the UI over https through a reverse proxy. | `*` / unset |

### Deployment and database

| Variable | Meaning | Default |
|---|---|---|
| `WEB_PORT` | Host port the catalog is served on. | `8000` |
| `DEBUG` | Django debug mode. Compose defaults it off. | `False` under compose |
| `POSTGRES_DB` / `POSTGRES_USER` | Database name and user. | `homelabvault` |
| `DATABASE_URL` | Connection string. **Required, no SQLite fallback.** Compose assembles it from the `POSTGRES_*` values; set it directly only outside compose. | assembled by compose |

### Health checks

| Variable | Meaning | Default |
|---|---|---|
| `HEALTH_DEFAULT_INTERVAL_S` / `HEALTH_DEFAULT_TIMEOUT_MS` / `HEALTH_DEFAULT_FAIL_THRESHOLD` | Defaults stamped onto services created before v1.3. The **live** interval and threshold are edited in Settings, not here. | `60` / `5000` / `3` |

### Notifications

| Variable | Meaning | Default |
|---|---|---|
| `HOMELABVAULT_DISCORD_WEBHOOK_URL` | Discord webhook — a credential, so it lives in `.env` and is never stored in or returned by the app. Turn notifications on in Settings. Unset is fine. | unset |

### Proxmox (optional)

| Variable | Meaning | Default |
|---|---|---|
| `PROXMOX_HOST` | PVE API base, **scheme required** — `https://<host>:8006`. Unset disables enrichment entirely. | unset |
| `PROXMOX_API_TOKEN` | The complete `PVEAPIToken=USER@REALM!TOKENID=UUID` string. Read-only (`PVEAuditor`) is sufficient — see `.env.example` for the exact `pveum` commands and the privilege-separation trap. | unset |
| `PROXMOX_VERIFY_TLS` | Off by default: Proxmox ships a self-signed certificate on 8006. Explicit and named, never a hidden `verify=False`. | `False` |
| `PROXMOX_TIMEOUT_S` | Per-request timeout against the cluster. | `15.0` |

### Probe / SSRF policy

| Variable | Meaning | Default |
|---|---|---|
| `PROBE_ALLOWED_NETWORKS` | Comma-separated CIDRs the probe and health checker may reach. **Security control — not UI-editable.** | `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10` |
| `PROBE_VERIFY_TLS` / `PROBE_TIMEOUT_S` | TLS verification (off — homelab self-signed certs) and per-fetch timeout. | `False` / `5.0` |
| `PROBE_MAX_REDIRECTS` / `PROBE_MAX_BYTES` / `PROBE_MAX_ICON_BYTES` / `ICON_MAX_INLINE_BYTES` | Redirect, size and icon budgets; every redirect hop is re-validated. | `3` / `262144` / `524288` / `32768` |

`100.64.0.0/10` is in the default allowlist because Tailscale assigns from that
CGNAT range. Removing it rejects every tailnet-addressed service in the lab.

### Recovery export

| Variable | Meaning | Default |
|---|---|---|
| `STATIC_EXPORT_DIR` | **Host** directory the recovery HTML is written to (bind-mounted to `/export`). Point it at a mounted share if you want the file to survive this host being down. | `./_export` |
| `STATIC_EXPORT_ENABLED` / `STATIC_EXPORT_MAX_ICON_BYTES` | Toggle, and the largest icon inlined into the export. | `True` / `98304` |

Four more variables are container paths that **compose sets itself** —
`STATIC_EXPORT_PATH`, `STATIC_EXPORT_STALE_MARKER`, `MEDIA_ROOT`, `CACHE_DIR`.
They exist to be set only when running the backend **outside** compose;
`.env.example` documents them alongside `DATABASE_URL` for that case.

## API

The auto-generated OpenAPI schema **is** the API documentation — there is no
hand-written reference to drift out of date.

- Browsable docs: <http://localhost:8000/api/v1/docs>
- Schema: <http://localhost:8000/api/v1/openapi.json>

**Everything lives under `/api/v1/`.** The prefix is reserved, not operated:
one API, no version negotiation, no dual maintenance — the app and its API ship
as one artifact and move together on every pull. Unprefixed `/api/*` paths
answer a **307** redirect, which preserves the method and body, so a bookmark
or a forgotten script still works. New callers should use `/api/v1/` directly.

Reads are unauthenticated. Writes need a bearer token or an admin session cookie.
The minimum valid payload is a single field:

```bash
curl -X POST http://localhost:8000/api/v1/services \
  -H "Authorization: Bearer $HOMELABVAULT_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"launch_url": "https://karakeep.lab.local"}'
```

The slug is derived from the URL and is the upsert key: running that command
twice updates one record rather than creating two.

## Security

**Reads are unauthenticated by design.** The catalog has to open as a browser
home page with no interstitial — that is the entire point.

The consequence is that **anyone with network access can read a complete
inventory of the lab** — service names, URLs, and host locations. So:

> **Homelab Vault must never be exposed beyond your private network without
> adding real authentication.**

`/api/v1/probe` fetches user-supplied URLs from inside the network, so it is
admin-scope only, restricted to `http`/`https`, blocked from loopback,
link-local and cloud-metadata addresses, capped at 3 redirects with every hop
re-validated, and size- and time-bounded. Favicons are fetched only from the same
origin as the probed page.

URL fields are validated at write time: launch, external, repo and docs URLs
must be full `http(s)` URLs, health URLs may also be `tcp://`, and icons are
limited to http(s), base64 `data:image/…`, or the app's own cache path — so a
stored `javascript:` URL cannot exist, in this UI or any future one.

## Backup and restore

Two independent mechanisms, because the tool lives inside the infrastructure it
catalogs.

**Nightly database backup** — `ops/backup.sh` runs `pg_dump` inside the `db`
container (via `docker compose exec`, so it needs no host port, no host
`pg_dump` and no password), gzips it, verifies the file is non-empty and
gzip-intact, and retains multiple generations. It writes to `./backups` by
default; set `BACKUP_DIR` to a mounted share so backups survive the host. The
cron line is in the script's header.

The dump is gzipped **plain SQL** — restore it with `psql`, never `pg_restore`
(which reads only pg_dump's custom formats). Into an **empty** database, with
the app stopped:

```bash
docker compose stop web worker proxmox-worker
docker compose exec db psql -U homelabvault -d postgres \
  -c "DROP DATABASE homelabvault;" -c "CREATE DATABASE homelabvault OWNER homelabvault;"
gunzip -c backups/homelabvault-<timestamp>.sql.gz \
  | docker compose exec -T db psql -U homelabvault -d homelabvault
docker compose start web worker proxmox-worker
```

A backup that has never been restored is an assumption, not a backup — do the
restore test once.

**Static recovery export** — every catalog change regenerates a single
dependency-free HTML file under `STATIC_EXPORT_DIR` listing every service name
and launch URL, with icons inlined as base64. Open it with `file://` when
Homelab Vault is down; every link in it still works. The export is written
outside the database transaction and can never block or fail a catalog write.
If an export attempt fails, the catalog write still succeeds, a stale marker is
recorded, and the health worker retries on its next cycle.

## Monitoring

`GET /api/v1/healthz` answers **503 while any background worker is stalled**,
200 otherwise — point any uptime monitor at it. Polling it is also what drives
the app's own stall evaluation, so an external poller arms the internal alerts.

`ops/watchdog.sh` is a self-contained cron script that does exactly that from
outside the containers, and additionally alerts (via the same Discord webhook)
when the whole stack is unreachable — the one failure the app cannot report
about itself. Install: one cron line, documented in the script's header.

## License

MIT — see [LICENSE](LICENSE).
