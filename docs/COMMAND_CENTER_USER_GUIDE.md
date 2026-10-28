# Command Center User Guide

**Last Updated**: 2026-10-28

## What it is

A dashboard at `/dashboard` showing service health and infrastructure at a
glance. Reach it from **Command Center**, the first item in the sidebar. The
Service Catalog is unchanged and still lives at `/catalog`.

The dashboard is organised as an **adapter bar**: a row of pills under the
header that switches between an **Overview** and one section per infrastructure
provider you have configured — **Proxmox** and **TrueNAS**.

This guide covers the screen itself. Setting up a provider, and what its
section's numbers mean, are in that provider's own guide:
[Proxmox](PROXMOX_USER_GUIDE.md) and [TrueNAS](TRUENAS_USER_GUIDE.md).

<!-- SCREENSHOT PLACEHOLDER: docs/images/dashboard.png — recapture: dark theme,
     desktop, /dashboard Overview with the adapter bar. The current file predates
     the adapter bar (it is also the README hero image). -->

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
catalog — including infrastructure figures for hardware that is not itself in
the catalog. This was a deliberate choice so the dashboard is complete without
signing in; it assumes the app is reachable only from your own network. Do not
expose Homelab Vault beyond it.

Some things stay off the public page even so — your VM and container names, the
reason a poll failed, TrueNAS disk serial numbers and the full text of TrueNAS
alerts. Each provider guide lists exactly what is shown and what is not:
[Proxmox](PROXMOX_USER_GUIDE.md#who-can-see-what),
[TrueNAS](TRUENAS_USER_GUIDE.md#who-can-see-what).

## The provider sections

- **Proxmox** (`/dashboard/proxmox`) — a cluster summary (health, compute,
  storage) and a table of nodes. What each value means, including the ones that
  are easy to misread, is in the
  [Proxmox User Guide](PROXMOX_USER_GUIDE.md#what-you-see-on-the-command-center).
- **TrueNAS** (`/dashboard/truenas`) — pool health, system and capacity cards,
  then tabs for pools, disks, backups and alerts. See the
  [TrueNAS User Guide](TRUENAS_USER_GUIDE.md#what-you-see-on-the-command-center).

**Settings → Panels** switches individual panels off — two for Proxmox, four for
TrueNAS. A panel switched off is removed from the page, not just hidden.

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

Provider-specific problems — a token that sees nothing, SMART reading unknown,
an alert that did not reach Discord — are in the
[Proxmox](PROXMOX_USER_GUIDE.md#troubleshooting) and
[TrueNAS](TRUENAS_USER_GUIDE.md#troubleshooting) guides.

## Related

- [Proxmox User Guide](PROXMOX_USER_GUIDE.md)
- [TrueNAS User Guide](TRUENAS_USER_GUIDE.md)
- [Settings User Guide](SETTINGS_USER_GUIDE.md)
- [Service Catalog User Guide](CATALOG_USER_GUIDE.md)
