# Changelog

All notable changes to Homelab Vault are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

**One artifact, one version.** The Django backend, the Vue SPA and the API ship
together and move together on every pull (spec §3). A release is the whole
application — there is no separate API version, and `/api/v1/` is a *reserved*
prefix rather than an operated one.

## The breaking-change convention

This file exists to hold this section. It was written before the first breaking
change rather than during one, because a policy that only becomes relevant when
nobody is thinking about it has to already exist by then.

**A change is breaking if it does any of these:**

- removes or renames an API field
- removes or moves an endpoint
- changes a response shape
- adds a required environment variable

**Three rules:**

1. Every breaking change gets its own `### Breaking` heading under the version
   it lands in. Not a bullet among the others — its own heading, so it is
   visible while skimming.
2. **Breaking changes land only on a major version.**
3. **A removed field must fail loudly.** Returning `null` where a value used to
   be is the worst option available: the caller sees a plausible value, not an
   error, and the failure surfaces somewhere else entirely as what looks like a
   bug in their own code. Fail with a message that names the field and the
   version that removed it.

A change that keeps existing callers working is not breaking, even if it moves
things — see the `/api/v1/` entry below, which is exactly that case.

---

## [1.0.0] — 2026-08-10

First public release.

### Breaking

- **URL columns are validated at write time.** `launch_url`,
  `launch_url_external`, `repo_url` and `docs_url` must be full `http://` or
  `https://` URLs including the scheme; `health_url` additionally accepts
  `tcp://` (that is how the tcp checker is selected); `icon_url` accepts an
  http(s) URL, a base64 `data:image/…` URI, or the app's own cached-icon
  path. Anything else — including a scheme-less `192.168.0.10:8096`, which
  had been accepted since v0 — now returns a **422 naming the field**.
  Nothing is silently normalized: a 422 that says the fix teaches the
  caller, a mutated value hides typos.

  Why: a URL column is something a browser eventually navigates, and
  `<a href="javascript:…">` executes with this app's CSRF token readable by
  design. Every render surface already refused such values independently;
  validating at the write path means the database cannot hold one, so no
  future surface can forget.

  Existing rows are **repaired by migration**, not grandfathered:
  scheme-less values were already broken (a digit-led `IP:port` resolved as
  a relative link to an in-app 404; a `host:port` parsed its host as the URL
  *scheme* and rendered as dead text), so the migration prepends `http://`
  where the result is a valid http URL, and clears an `icon_url` no surface
  would render. Values that are not a typo (`javascript:` and kin) are left
  in place — they were never navigable — and the row's next edit returns a
  422 naming the field.

### Fixed

- A malformed percent-escape in a service link (`/services/%zz`) no longer
  blanks the whole app on a cold load — it lands on the "no service with that
  address" card the router always meant to show.
- Escape closes only the topmost modal, so dismissing the admin-token prompt no
  longer also destroys the filled form beneath it.
- Failing writes say so: save view, bulk tag, bulk category and the row
  recheck now show the server or network error instead of silently doing
  nothing.
- The Command Center header says **"Not refreshing — server unreachable"**
  when a background refresh fails, instead of silently showing aging numbers
  over the last good data.
- The admin-token prompt sends the token trimmed, so a paste with a trailing
  newline no longer fails with an unexplained "Not authorised."
- A slow catalog poll that was overtaken by a save (or a newer poll) is
  discarded instead of briefly reverting the change on screen.
- Bulk import handles the same URL pasted twice: the rows stay distinct, and
  when one twin imports and the other fails, the failed one stays listed.

### Added

- **A `LICENSE`** — MIT.
- **CI** — GitHub Actions running both suites (the backend twice, back to
  back), the Django system and migration-drift checks, the frontend build,
  full dependency audits, a secret scan over the whole history, and a
  container build + compose smoke.
- **Proxmox location enrichment** (optional) — a read-only PVE provider and a
  fourth compose service, `proxmox-worker`, that polls the cluster, caches
  guests in Postgres, and links catalog services to the guest that serves
  them. `location_derived` (e.g. `pve-c · CT 105`) appears on every service
  response; a link tracks its guest by VMID after the first address match, so
  it survives DHCP lease changes and migrates with the guest across nodes. An
  address claimed by more than one guest (every Docker host's `172.17.0.1`)
  is dropped rather than guessed at. A pinned `host_label` is never
  overwritten: a disagreement is displayed, and unpin is the only action.
  With `PROXMOX_HOST` unset, none of this exists and nothing else changes.
- **Command Center dashboard** — a status summary panel (works with no
  provider configured) and a Proxmox nodes panel, assembled server-side from
  the worker's cached snapshot behind one endpoint, `GET /api/v1/panels`; a
  panel read never makes an outbound call. Storage is deduplicated across
  nodes (a shared NFS mount counts once, per-node disks count each) and the
  PVE version is reported per node, since a mid-upgrade cluster has more than
  one.
- **Real URLs** — `/catalog`, `/dashboard`, `/settings` and
  `/services/{slug}` are served by Django on a cold load, so a reload keeps
  your place and unknown paths 404. `ui.landing_page` in Settings decides
  what `/` opens.
- **Saved views can be renamed** — `PATCH /api/v1/views/{name}`, agent scope.
  Renaming onto an existing view is a 422, deliberately unlike the category
  rename (which merges): two views are two stored queries, and a merge would
  silently discard one.
- **`location_stale` on every service response** — `true` once the provider has
  answered three consecutive polls without the linked guest, while
  `location_derived` still carries the last known value: stale is said, not
  blanked. After twenty such polls the link is dropped and the service returns
  to address matching — a destroyed guest no longer reports its old location
  forever, and a VMID recycled onto an unrelated guest is refused unless its
  name or an address corroborates it. **Not breaking**: a new response field.
  Rendered since audit fix window 3 as a **stale** tag beside the derived
  location in the catalog and on the service record — never beside a pin.
- **`privileges` on the admin provider status row** — the grants the Proxmox
  credential actually held at the last successful poll, beside the row that
  says whether it works. Admin-scoped; the public panels payload is pinned
  free of it. Rendered on the Settings provider card since audit fix window 3.
- **Discord messages are structured embeds** with a fixed field schema — Event,
  Was down for (on a recovery), URL, Category, Tags, the slug in the footer —
  plus the previous one-line text as the message content, which is what a push
  preview shows. The schema is deliberately code-fixed so an agent watching the
  channel can parse the same fields every time. Only `http(s)` launch URLs
  appear in the URL field. **Not breaking**: the webhook payload gains an
  `embeds` array; Discord ignores nothing and old messages needed no consumer.
- **Mention a configured user and/or role on every notification** —
  `notifications.discord.mention_user` / `mention_role` in Settings, a new
  `discord_mention` settings kind accepting a 17–20 digit Discord ID or empty.
  Implemented as an explicit `allowed_mentions` allowlist; mention *parsing*
  stays off, so text in a probed service name (`@everyone`) can never ping.
- **A Discord alert when a provider poll keeps failing** — one message after 3
  consecutive failed polls, one on recovery, gated on the existing Discord
  toggle. The message carries no diagnostic; the settings screen does.
- **`infrastructure_detail` on the admin provider status row** — why the
  cluster read failed while the guest list still worked, closing the "see
  Settings for the reason" dead end. Admin-scoped; the public panels payload is
  unchanged.
- **`ui.catalog.density`** (`compact` default / `comfortable`) — larger catalog
  rows, icons and text, server-wide, on the public settings whitelist.
- **Discord notifications** — a service is announced when it goes down and when
  it recovers, and a background worker is announced when it stops reporting.
  Off by default; switch it on in Settings > Notifications. The webhook URL is
  read from `HOMELABVAULT_DISCORD_WEBHOOK_URL` in `.env` and is never stored in,
  displayed by, or returned from the application. **Not breaking**: unset, the
  app logs that it is unset and carries on.
- **`notify_enabled` on a service** (default `true`, editable on the service
  record) — separate from `health_enabled`. A service can be loud on the
  dashboard without waking you: it stays checked and still shows its real
  status, it just never posts.
- **`GET /api/v1/healthz`** — per-worker freshness for an external monitor.
  **503** while any registered worker is stalled, **200** otherwise. Anonymous
  callers get liveness; thresholds and process internals need the admin token.
  Polling it also runs the stall check, so an external watcher drives the
  internal one. Alerts are edge-triggered — once per stall, once per recovery.
- **`manage.py deadman`** — the same evaluation as a one-shot command, for a
  cron entry. `--loop` runs it on a timer instead.
- **`health_interval_effective_s` on a service response** — the interval
  actually in force, which is the service's own or the global one. The catalog
  re-derives staleness in the browser and needs it.

- **Settings screen** — runtime preferences (landing page, panel visibility,
  health-check thresholds, notification and Proxmox toggles) in a `settings`
  table, behind `GET`/`PATCH /api/v1/settings`. Reads are admin-scoped; a narrow
  `GET /api/v1/settings/public` returns an explicit whitelist of display keys
  for anonymous viewers. Deploy-time configuration stays in `.env` and the
  screen shows it read-only, with the variable name and no affordance.
- **`HOMELABVAULT_AUTH_MODE`** — `full` (default), `open-writes`, or `open`.
  **Not breaking**: absent, it is `full`, which is v0 behaviour exactly. It
  fails closed, so a misspelled value is also `full`. Probe, icon and settings
  endpoints stay admin-only in every mode.
- **`ADMIN_REAUTH_WINDOW_S`** (default 300) — the four destructive `DELETE`
  endpoints ask for the admin token again if it was last presented longer ago
  than this. Bearer callers are exempt: the credential is on the request.

### Changed

- **The build is reproducible.** Base images are pinned by digest; the
  frontend installs with `npm ci` against a mandatory lockfile instead of
  `npm install` against an optional one; Python dependencies install with
  `--require-hashes` from a committed, fully-pinned lock
  (`backend/requirements.lock`), which now also carries `gunicorn` instead of
  it being installed unpinned in the Dockerfile.
- **Example data is neutral.** Test fixtures, UI placeholders and
  configuration examples use generic node names (`pve-a`…`pve-d`), default-
  subnet and documentation-range addresses, and a neutral example PVE host.
- **The dev toolchain is current** — Vitest 4 / Vite 7 / jsdom 27; `npm
  audit` (dev dependencies included) reports zero vulnerabilities, and CI
  fails on high or critical from now on.
- **A recovery is damped like a failure.** A service returning to `up` now waits
  for `health.recovery_threshold` successful checks (default 2) instead of the
  first one. Without it, one flapping service posts on every bounce and the
  usual outcome is a muted channel — which mutes the real alerts too. A service
  being checked for the first time is *not* damped and is not announced.
- **A service's poll interval and fail threshold may now be empty**, meaning
  "follow the values in Settings". They used to be stamped with a copy of the
  default when the service was created, so no service could follow anything.
  Existing services were unstamped only where the stored value matched the
  default that stamped them; a value anybody chose stays put. **Not breaking**:
  the fields keep their names and types, and `health_interval_effective_s`
  carries the resolved number.

- **All API endpoints moved to the `/api/v1/` prefix.** The schema is now at
  `/api/v1/openapi.json` and the browsable docs at `/api/v1/docs`.
- Unprefixed `/api/*` paths answer **307**, which obliges the client to replay
  its original method and body. **Not breaking**: an old caller's POST is still
  a POST when it arrives. 301/302 would have permitted clients to turn writes
  into reads and drop the body — a write that reports success and does nothing.

### Security

- **Every writable text field now has a length ceiling, and tags have count and
  length caps** — name/category/host label 200, slug 100, URLs 2000,
  description 4000, 32 tags of 100 characters, `icon_url` derived from the
  inline-icon byte cap — refused with a 422 naming the field on create, patch,
  the taxonomy renames and saved views alike, plus a 2000-record catalog
  ceiling that refuses new records only (updates keep working). Unbounded
  fields were a persistent resource-exhaustion primitive under `open-writes`:
  the public catalog re-serves the whole collection every 15 seconds, so
  parked megabytes kept costing after the writer stopped. **Not breaking**
  under the convention above — no field or endpoint changed shape; input no
  legitimate caller sends is now refused, and the ceilings sit far above
  human-scale data.
- **Favicon discovery has a budget.** At most 8 icon candidates are attempted
  per probe under a wall-clock ceiling, a malformed `<link href>` port
  (`:99999`) is a rejected candidate instead of an unhandled crash, the same
  malformed port in a probed or checked URL is refused by the SSRF validator
  with a clean error, and a `tcp://` health URL with an out-of-range port is a
  failed check rather than an exception. A probed page could previously decide
  how long the probe ran and could 500 the admin probe endpoint.
- **A saved view's `position` is bounded** (0–1,000,000) — views skip model
  validation, so an out-of-range integer was a database error surfacing as a
  500 instead of the contract's 422.

- **The offline recovery export no longer trusts `icon_url` or `launch_url`.**
  The icon cell inlines only base64 `data:image/…` URIs (whose alphabet cannot
  carry a quote) and is HTML-escaped; a `launch_url` that is not `http(s)` — a
  `javascript:` URL, say — is shown as plain text rather than a clickable link.
  Both were stored-injection primitives in a file the operator opens from the
  NAS as `file://` during an outage. `_accept_data_uri` on the probe path was
  tightened to the same base64-only rule, so a utf-8 SVG favicon is now refused
  (the service falls back to no icon).
- **`health_timeout_ms` is bounded to 250–60000 ms** on both the create and the
  patch path (a 422 names the field), clamped again worker-side for rows written
  around validation, and every health check now runs under a hard wall-clock
  ceiling. Unbounded values were a denial-of-service primitive: a huge timeout
  pinned a gunicorn worker through `POST /health/{slug}/check` (three requests
  took the app down), and a negative one failed every check at once and turned
  the whole catalog `down`. Reachable without a token under `open-writes`.
- **The health worker resolves DNS off its event loop.** A single unresolvable
  hostname previously blocked the loop for the resolver's timeout, serialising
  every concurrent check, pushing the pass past 3× interval, and tripping the
  deadman — a total monitoring outage from one ordinary DNS blip, no attacker
  required.

- **A per-service poll interval has a 15-second floor**, matching the one on the
  global setting. Pinning a service to a 1-second interval made the worker probe
  it 60 times a minute — a sustained amplification of a create's single outbound
  probe, and reachable without a token under `open-writes`.
- **The webhook value must start `https://`.** The settings table names *which*
  environment variable holds it, so a wrong name is one keystroke away — and
  without this check a variable holding a credential would be put in a request
  line and sent to whatever host it named.

- **Rotating `HOMELABVAULT_ADMIN_TOKEN` now signs out live browser sessions.**
  Previously it locked out bearer callers and left every browser session
  working — backwards for a credential rotation. Sessions store a fingerprint
  of the token they were minted from, never the token. Note that rotation
  *invalidates*; it does not erase: an untouched session row survives until it
  expires, and is flushed the moment it is next served.
- Session expiry is confirmed to be a fixed window from login rather than
  sliding, and is now covered by a behavioural test.

### Removed

- **SQLite support.** PostgreSQL is the only backend, in development as well as
  production. `DATABASE_URL` is required and has no fallback.

  > Not filed as `### Breaking` only because nothing had been released against
  > the dual-backend build. Under the convention above, this shape of change —
  > a newly required environment variable — is breaking, and a real one would
  > wait for a major version.

### Fixed

- **`ops/backup.sh` can actually run against the compose stack.** It now
  dumps inside the `db` container via `docker compose exec` instead of
  dialling a compose-internal hostname from the host (which never resolved,
  and had no password to authenticate with if it had), and it defaults to
  `./backups` instead of a machine-specific NAS path. The restore
  instructions were corrected everywhere they appear: the dump is gzipped
  **plain SQL**, restored with `psql` — the previous instruction named
  `pg_restore`, which cannot read that format at all.
- **The README describes the product that ships** — four compose services,
  the mandatory `_export` ownership step before first start, the Settings
  screen, the no-Proxmox default path stated first, and a complete
  configuration reference (the auth-mode, session, Discord, Proxmox and
  compose-internal variables it previously omitted).
- **The private-path ignore rules travel with the repository** — moved from
  `.git/info/exclude`, which no clone receives, into the tracked
  `.gitignore`.
- **A Proxmox answer that is not the PVE API is now a recorded failed poll.**
  A 200 carrying HTML or the wrong JSON shape — a reverse proxy's error page,
  a changed endpoint — used to crash past failed-poll recording: the worker
  logged it and kept beating, so the snapshot stayed green with stale data and
  Settings showed nothing. The poll boundary now catches everything, records a
  closed-string reason (never the response body), counts it toward the
  provider alert, and one poll makes one `/access/permissions` read instead of
  three.
- **Two agents registering the same new slug at once both succeed.** The loser
  of the insert race now updates the winner's record and answers 200 instead
  of raising a 500 — the idempotency the upsert promises, on exactly the
  concurrent-reconciliation workload that produces identical registrations.
- **Static exports no longer race each other.** Exports are serialised with a
  Postgres advisory lock across every web worker and the health worker's
  retry; a burst of mutations coalesces into at most one follow-up export; a
  temp file is unique per attempt; and an overtaken export can no longer
  overwrite newer data or clear a newer failure's stale marker.
- **Migration 0003 uses the historical defaults it shipped with** (60 s / 3)
  instead of reading the live environment, so upgrading with a changed
  `HEALTH_DEFAULT_*` no longer mis-classifies which rows were deliberately
  pinned.
- **`HOMELABVAULT_AUTH_MODE` and `ADMIN_REAUTH_WINDOW_S` now reach the web
  container under Docker.** They were documented in `.env.example` but never
  listed in `docker-compose.yml`'s `environment:` block, so Compose never passed
  them in — every Docker deployment silently ran `full` regardless of `.env`,
  with nothing logged. Setting `open-writes` works for the first time.
- Connection hygiene at thread boundaries: library code no longer tears down a
  connection whose transaction it does not own, and worker threads release
  their own sessions.

## [0.1.0] — 2026-08-03

First working system.

### Added

- **Service catalog** — register, search and launch services, with the query
  syntax, a command palette, and saved views.
- **Health-check worker** — per-service intervals, an `http` and a `tcp`
  checker, and threshold-based state transitions.
- **URL probing with icon discovery** — SSRF-hardened: scheme allowlist,
  address policy re-validated on every hop, admin scope only.
- **Offline recovery export** — one self-contained HTML file with icons
  inlined, rewritten on every commit, so the catalog is readable while the app
  itself is down.
- **Bearer and session auth** — `agent` and `admin` scopes, with CSRF enforced
  on the cookie branch only.
