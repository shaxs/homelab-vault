# TrueNAS User Guide

**Last Updated**: 2026-10-28

## What it is

An optional, read-only connection to a **TrueNAS SCALE** box. Once it is set
up, Homelab Vault:

- shows your pools, disks, backups and the box's own alerts in a **TrueNAS
  section** of the Command Center,
- **forwards TrueNAS's own critical alerts to Discord** — a failing disk, a
  degraded pool — and says again when each one clears, and
- tells you when it can no longer reach the box.

**Why this exists: TrueNAS watches your storage, and Homelab Vault watches that
it did.** The feature was built after a disk degraded for months unnoticed,
because nobody was looking at TrueNAS often enough. TrueNAS had the alert; nobody
saw it. The point is to put that alert somewhere you already look.

Nothing here ever changes anything on the box. With TrueNAS left unconfigured,
there is no TrueNAS pill on the dashboard, nothing polls it, and the rest of the
app behaves exactly the same.

It was built and tested against **TrueNAS SCALE 25.04**.

## Before you start: never send the key over plain HTTP

> **TrueNAS 25.04 and later *revoke* any user-linked API key they see on a
> plain-HTTP connection.** The request does not simply fail — the key is
> destroyed and you have to create a new one.
>
> Homelab Vault always connects over TLS (`wss://`), whatever scheme you write in
> `TRUENAS_HOST`, so the app itself cannot trip this. But never test the key by
> hand with `curl http://…` or anything else unencrypted.

## Setting it up

### 1. Create a read-only service account on TrueNAS

API keys on TrueNAS 25.04+ are **linked to a user**, so the key needs a user to
belong to. Give it a dedicated one with read-only rights rather than using your
own admin account:

1. **Credentials → Groups** — note the built-in
   **`truenas_readonly_administrators`** group. It carries the **Read-Only
   Admin** role.
2. **Credentials → Users → Add** — create a user, for example `homelabvault`.
   It needs no shell access. Add it to that group.
3. On that user: **API Keys → Add**. Copy the key straight away — TrueNAS shows
   it once.

### 2. Add three values to `.env`

```text
TRUENAS_HOST=https://192.168.0.50
TRUENAS_API_USER=homelabvault
TRUENAS_API_KEY=<the key you copied>
```

| Variable | What it holds |
|---|---|
| `TRUENAS_HOST` | The box's address, with a scheme. The app connects over TLS whatever the scheme says. |
| `TRUENAS_API_USER` | The user the key belongs to. The key alone does not identify the account on 25.04+. |
| `TRUENAS_API_KEY` | That user's API key. Never shown on screen or returned by the API — the app only ever reports whether it is set. |
| `TRUENAS_VERIFY_TLS` | Optional, default `False`. See [The certificate warning](#the-certificate-warning). |
| `TRUENAS_TIMEOUT_S` | Optional, default `15.0` seconds per request. |

### 3. Restart and switch polling on

```bash
docker compose up -d
```

Then open **Settings → TrueNAS** and turn on **TrueNAS polling**. It is **off by
default**, even with all three values set. The **poll interval** defaults to
120 seconds (minimum 30). Both take effect on the worker's next cycle — no
restart.

TrueNAS has its own worker, `truenas-worker`, separate from the Proxmox one, so
one provider being slow or down never affects the other. It is safe to leave
running with TrueNAS unconfigured; it simply does nothing.

## Checking that it works

**Settings → TrueNAS** shows what the last poll found:

| It says | It means |
|---|---|
| **Not configured** | None of the three values are set. A choice, not a fault, and it never alerts. |
| **Partly configured — missing …** | Some are set and some are not; it names the missing variable. Usually an older setup that predates `TRUENAS_API_USER`. |
| **Waiting for the first poll** | Configured, polling just turned on. Give it one poll interval. |
| **Working** | The box answered on the last poll. |
| **Not working**, with a reason | The last poll failed. The reason is shown here and only here — the public dashboard just says to look in Settings. |

The sidebar's worker list also shows the TrueNAS worker's own heartbeat, next to
the health and Proxmox workers.

## What you see on the Command Center

<!-- SCREENSHOT PLACEHOLDER: docs/images/dashboard-truenas.png — dark theme,
     desktop, /dashboard/truenas with the pill strip and the Alerts tab open. -->

**On the Overview** (`/dashboard`), a TrueNAS tile shows pools healthy, capacity
used and active critical alerts. Clicking it opens the TrueNAS section.

**The TrueNAS section** (`/dashboard/truenas`) shows the box's hostname and a
health pill, then three summary cards:

1. **Pool health** — Online or Degraded, the count of healthy pools, and any
   running scrub or resilver with its progress.
2. **System** — load average, RAM (total, and used where TrueNAS reports it),
   uptime and version.
3. **Capacity** — used and total across all pools, with a line per pool.

Below the cards, a tabbed table:

- **Pools** — name, status, capacity, used % and scrub state.
- **Disks** — name, model, type, owning pool, SMART status (green **PASS**, red
  **FAIL**) and temperature. **Serial numbers are never shown.**
- **Backups** — replication and cloud-sync tasks: name, kind, last state (green
  for finished, red for failed) and when it last ran.
- **Alerts** — the box's active alerts, each with a severity pill (amber for
  warnings, red for errors and above), a short **category** — the alert *type*,
  like "Smart Error" or "Zpool Capacity Notice" — and when it was raised.
  Alerts you have dismissed in TrueNAS are left out.

Clicking any row opens a details panel on the right.

**Settings → Panels** can switch off four of these: **TrueNAS System**,
**TrueNAS Pools**, **TrueNAS Disks** and **TrueNAS Alerts**. A panel switched
off is not merely hidden — its data is never sent to the browser. Backups have
no switch and always show when TrueNAS reports any.

The rest of the screen — the adapter bar, the health dot on each pill, the phone
layout — is covered in the [Command Center User Guide](COMMAND_CENTER_USER_GUIDE.md).

## Alerts in Discord

With **Settings → Notifications → Discord notifications** on, TrueNAS can post
three kinds of message:

| What happened | What Discord gets |
|---|---|
| **TrueNAS raised an alert at ERROR or above** (ERROR, CRITICAL, ALERT, EMERGENCY) | One message as it appears, with the alert's level and text. One more when it clears. |
| **The poll has failed 3 times in a row** | One message saying the provider is failing, and one when it recovers. A single failed poll is a blip and is announced to nobody. |
| **The TrueNAS worker stopped** | The same stall alert every worker gets, once when it stalls and once when it comes back. |

Lower-level alerts (INFO, NOTICE, WARNING) still appear in the Alerts tab; they
just do not post.

Alerts are **edge-triggered**: each alert posts once when it appears and once
when it clears, never once per poll. An alert that sits on the box for a week is
one message, not two thousand — a channel that floods gets muted, and then it
misses the one that matters.

The alert text that reaches Discord is cleaned first — markdown escaped,
control characters removed, length capped — and can never trigger a mention, so
a dataset named `@everyone` posts as plain text. Your configured mention
(**Mention user ID** / **Mention role ID**) still pings as usual.

## Who can see what

**The dashboard is readable by anyone who can reach Homelab Vault**, the same as
the catalog. For TrueNAS that means pool names, disk models, capacity, system
figures, backup task names and the *type* of each active alert. That is
deliberate, so the dashboard works without signing in, and it assumes the app is
reachable only from your own network.

Two things never reach the public page:

- **Disk serial numbers.** They are dropped as soon as they are read and are
  never stored, shown or returned by the API.
- **The full text of an alert.** Alert text can name pools and datasets, so the
  dashboard shows only the severity and category. The full text goes to Discord
  (cleaned, as above) and is always available in the TrueNAS UI itself.

The *reason* a poll failed is admin-only, on the Settings screen.

## Things worth knowing

- **SMART status can read "unknown" for every disk, and that is not a disk
  fault.** SMART results are read on a best-effort basis. On TrueNAS
  **25.04.2.6** the box's own API fails when asked for them (a bug on the
  TrueNAS side), so every disk shows SMART unknown on that release. The
  authoritative signals for a failing drive are **pool health** and TrueNAS's
  **own alerts**, and both keep working.
- **Some figures depend on the release.** RAM *used* only appears where TrueNAS
  reports it. A row with a dash in a column means that item did not answer one
  of its reads; the rest of the row is still accurate.
- **Read-only, end to end.** The account only holds the Read-Only Admin role,
  and Homelab Vault only ever asks for information.

### The certificate warning

TrueNAS usually presents a self-signed certificate, so `TRUENAS_VERIFY_TLS`
defaults to `False` and the server log warns about it once per process. The
connection is still encrypted; the setting only controls whether the
certificate is checked. If your TrueNAS has a certificate your machine trusts,
set `TRUENAS_VERIFY_TLS=True` — the API key travels on every request.

## Troubleshooting

| What you see | What to do |
|---|---|
| No TrueNAS pill on the dashboard | The three values are not all set, or the server has not been restarted since. Check **Settings → TrueNAS**. |
| *"TrueNAS polling is switched off in Settings."* | Turn on **Settings → TrueNAS → TrueNAS polling**. |
| *"TrueNAS state is unavailable — see Settings for the reason."* | The poll failed. **Settings → TrueNAS** shows the actual reason. |
| **Partly configured — missing TRUENAS_API_USER** | Add the username the key belongs to, then restart. Keys on 25.04+ cannot be used without it. |
| The key suddenly stopped working | It may have been revoked — TrueNAS does this if the key is ever sent over plain HTTP, by anything. Create a new key on the same user and update `TRUENAS_API_KEY`. |
| Every disk says **SMART unknown** | Expected on TrueNAS 25.04.2.6 (see above). Pool health and alerts are unaffected. |
| An alert shows on the dashboard but nothing came to Discord | It is below ERROR, or Discord notifications are off, or it was already announced — each alert posts once. |
| TrueNAS went down and Discord said nothing | Fewer than 3 polls have failed so far, or notifications are off. Wait three poll intervals. |
| The server log warns about `TRUENAS_VERIFY_TLS` | Expected with a self-signed certificate. See [The certificate warning](#the-certificate-warning). |

## Related

- [Command Center User Guide](COMMAND_CENTER_USER_GUIDE.md) — the dashboard
  screen itself
- [Settings User Guide](SETTINGS_USER_GUIDE.md) — Discord notifications, panel
  switches, worker health
- [Proxmox User Guide](PROXMOX_USER_GUIDE.md) — the other infrastructure
  provider
