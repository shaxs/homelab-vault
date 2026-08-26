# Command Center User Guide

**Last Updated**: 2026-08-11

## What it is

A dashboard at `/dashboard` showing service health and infrastructure at a
glance. Reach it from **Command Center**, the first item in the sidebar. The
Service Catalog is unchanged and still lives at `/catalog`.

The dashboard is organised as an **adapter bar**: a row of pills under the
header that switches between an **Overview** and one section per infrastructure
provider you have configured — **Proxmox** and **TrueNAS**.

## Choosing what loads first

**Settings → Appearance → Landing page** picks whether `/` opens the Service
Catalog or the Command Center. The catalog stays reachable at `/catalog` either
way.

This setting is **server-wide**, not per-device — unlike the light/dark theme,
which each browser remembers for itself.

Every screen has its own address, so a bookmark or a link works after a reload:
`/catalog`, `/dashboard`, `/settings`, `/services/<slug>` for one service's
record, and now `/dashboard/proxmox` and `/dashboard/truenas` for a provider
section.

## The adapter bar

The adapter bar sits directly under the header. It holds an **Overview** pill
and one pill for each provider you have configured.

- **Switching sections** — click a pill to show that section. `/dashboard` is
  the Overview; `/dashboard/proxmox` and `/dashboard/truenas` are the sections,
  and each is bookmarkable.
- **Health dot** — each provider pill carries a small coloured dot: **green** is
  healthy, **amber** means something wants attention (stale data, a node down, a
  non-critical alert), **red** means degraded or a critical alert. The Overview
  pill has no dot.
- **Only configured providers appear.** With no TrueNAS credentials there is no
  TrueNAS pill; the bar shows only what you have set up.
- **On a phone** the pill strip scrolls sideways rather than folding into the
  menu, so every section stays one tap away.

## Overview

The `/dashboard` view is a high-level summary that works even with no provider
configured.

- **Status Summary** — counts of up, down, stale, unknown and "no check"
  services. Clicking a count filters the catalog by it. Below the counts, a
  **Needs attention** list names services that are down, stale or never checked,
  worst first, up to 5 with a count of any beyond that. Services with health
  checks switched off are never listed here — that is a choice, not a fault.
- **A tile per provider** — a couple of headline stats for each configured
  provider: for Proxmox, nodes online and cluster health; for TrueNAS, pools
  healthy, capacity used, and active critical alerts. Clicking a tile opens that
  provider's section.

## Who can see it

**The dashboard is readable by anyone who can reach the app**, the same as the
catalog. That includes Proxmox node names, node addresses, versions and CPU, RAM
and storage totals, and TrueNAS pool names, disk models, capacity, system
figures and the *type* of any active alert — for hardware that is not itself in
the catalog. This was a deliberate choice so the dashboard is complete without
signing in; it assumes the app is reachable only from your own network. Do not
expose Homelab Vault beyond it.

Some things stay behind the admin token even so:

- The names and addresses of your VMs and containers.
- The *reason* a poll failed, which can name an environment variable.
- **TrueNAS disk serial numbers**, which are never shown at all.
- **The full text of a TrueNAS alert.** The dashboard shows an alert's severity
  and short *type* (its category), not the message — a pool or dataset name in
  the raw text is kept off a public page. The full detail is one click away in
  the TrueNAS UI itself.

Those first two appear on the Settings screen instead.

## The Proxmox section

At `/dashboard/proxmox` when Proxmox is configured. **Settings → Panels**
switches each panel on or off; switching one off removes it from the page.

- **Proxmox Cluster Summary** — three cards: Cluster health (Healthy / Degraded /
  No quorum, plus Quorum and HA), Compute (online nodes, CPU %, RAM %) and
  Storage (online storages, total capacity, used, VM and LXC counts).
- **Proxmox Nodes** — one row per node: Node, Status, Uptime, CPU, RAM, Load avg,
  Storage, Version.

### What the values mean

- **HA: Not configured** means Proxmox reported no high-availability services.
  **Unknown** means the API token could not read HA status — it needs the
  `Sys.Audit` privilege. That is different from HA being off.
- **The Version column is per node, on purpose.** A cluster part-way through an
  upgrade shows different versions on different rows.
- **A node's Storage column counts only that node's local storage.** Storage
  shared across nodes — an NFS mount, for example — is counted once in the
  cluster Storage card instead, not once per node.
- **Cluster total capacity counts each storage once.** If one NAS pool is
  exported as several separate storages, each reports the whole pool's size, so
  the total can read high. Proxmox's own datacenter summary behaves the same way.
- **Cluster CPU % is weighted by core count**, so a small busy node does not drag
  the whole figure up.
- **"polled Ns ago"** on the Compute card is when the Proxmox worker last
  succeeded.
- **A "Not refreshing" badge** on the Nodes panel means the cached data is older
  than three poll intervals — usually the Proxmox worker has stopped.

## The TrueNAS section

At `/dashboard/truenas` when TrueNAS is configured. The header shows the box's
hostname and a health pill.

Three summary cards:

1. **Pool health** — Online or Degraded, the count of healthy pools, and any
   running scrub or resilver with its progress.
2. **System** — load average, RAM (total, and used where TrueNAS reports it),
   uptime and version.
3. **Capacity** — used and total across all pools, with a line per pool.

Below the cards, a tabbed table:

- **Pools** — name, status, capacity, used %, and scrub state.
- **Disks** — name, model, type, owning pool, SMART status (green for PASS, red
  for FAIL) and temperature. **Serial numbers are deliberately not shown.**
- **Backups** — replication and cloud-sync tasks: name, kind, last state
  (green for finished, red for failed) and when it last ran.
- **Alerts** — the box's own active alerts, each a severity pill (amber for
  warnings, red for errors and above) plus a short **category** — the alert
  *type*, like "Smart Error" or "Zpool Capacity Notice" — and when it was raised.
  **The full alert text is not shown here**; open TrueNAS for the detail (see
  *Who can see it* above).

Each of the four TrueNAS panels (System, Pools, Disks, Alerts) can be switched
off in **Settings → Panels**. Clicking any row opens the details panel on the
right.

## Clicking a row

Clicking a node, pool, disk, backup, alert, or a service under Needs attention
opens a details panel on the right. Rows the app cannot fill are left out rather
than shown as a dash.

A service shows its URL, host, cluster, version and uptime where those are known,
plus an **Open external** action. If a service's URL is not `http://` or
`https://` it is shown but not made clickable, and no action appears.

A Proxmox node offers **Open web UI**, which opens that node's own console at
`https://<node address>:8006`. If Proxmox did not report an address for a node,
the action is left out rather than pointing at a dead link.

## On a phone or tablet

Below desktop width the details panel slides in from the right as an overlay
instead of sitting beside the table; **✕** closes it. Nothing behind it is
blocked — the page stays scrollable while it is open.

The adapter bar scrolls sideways so every section stays reachable. The summary
cards stack vertically, the status summary tiles wrap to fit, and each Proxmox
node row becomes a card: the node's name and online/offline pill first, then
labelled facts — **up** (uptime), **cpu** and **ram** with their meter bars,
**load**, **disk** and **pve** (version). Each TrueNAS pool, disk, backup and
alert likewise becomes a labelled card, so nothing needs sideways scrolling.

## When a provider is not set up

With no credentials for a provider in `.env`, its pill does not appear and its
section is absent. Opening a section's URL directly — `/dashboard/truenas` with
no TrueNAS configured — shows a brief "not configured" note rather than an empty
page. If a provider is configured but polling is switched off in Settings, the
section appears and says so, rather than showing numbers that have quietly
stopped moving.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "Waiting for the first poll." | Configured, but the worker has not completed a cycle yet — wait one poll interval. |
| "Proxmox polling is switched off in Settings." | Turn on **Settings → Proxmox → Proxmox polling**. |
| "TrueNAS polling is switched off in Settings." | Turn on **Settings → TrueNAS → TrueNAS polling**. |
| "Cluster state is unavailable — see Settings for the reason." | The Proxmox poll failed. **Settings → Proxmox** shows the actual reason, kept off this page deliberately because the page is public. |
| "TrueNAS state is unavailable — see Settings for the reason." | The TrueNAS poll failed. **Settings → TrueNAS** shows the actual reason. |
| No pill for a provider | Its credentials are not set in `.env`. |
| A "Not refreshing — server unreachable" badge in the header | The browser cannot reach Homelab Vault itself; the page keeps the last data it loaded rather than going blank. |
| A node row, or TrueNAS row, with dashes in some columns | That item did not answer one of its reads. The rest of the row is still accurate. |

## Related

- [Settings User Guide](SETTINGS_USER_GUIDE.md)
- [Service Catalog User Guide](CATALOG_USER_GUIDE.md)
