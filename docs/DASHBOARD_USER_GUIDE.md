# Command Center User Guide

**Last Updated**: 2026-08-10

## What it is

A dashboard at `/dashboard` showing service health and Proxmox cluster state at a
glance. Reach it from **Command Center**, the first item in the sidebar. The Service Catalog is
unchanged and still lives at `/catalog`.

## Choosing what loads first

**Settings → Appearance → Landing page** picks whether `/` opens the Service
Catalog or the Command Center. The catalog stays reachable at `/catalog` either
way.

This setting is **server-wide**, not per-device — unlike the light/dark theme,
which each browser remembers for itself.

Every screen now has its own address, so a bookmark or a link works after a
reload: `/catalog`, `/dashboard`, `/settings`, and `/services/<slug>` for one
service's record.

## Who can see it

**The dashboard is readable by anyone who can reach the app**, the same as the
catalog. That includes node names, node addresses, Proxmox versions, and CPU, RAM and
storage totals — for hosts that are not themselves in the catalog. This was a deliberate
choice so the dashboard is complete without signing in; it assumes the app is
reachable only from your own network. Do not expose Homelab Vault beyond it.

Some things stay behind the admin token even so: the names and addresses of your
VMs and containers, and the *reason* a Proxmox poll failed, which can name an
environment variable. Those appear on the Settings screen instead.

## The panels

**Settings → Panels** switches each panel on or off. Switching one off removes it
from the page entirely.

- **Status Summary** — counts of up, down, stale, unknown and "no check"
  services. Clicking a count filters the catalog by it. Below the counts, a
  **Needs attention** list names services that are down, stale or never checked,
  worst first, up to 5 with a count of any beyond that. Services with health
  checks switched off are never listed here — that is a choice, not a fault.
- **Proxmox Cluster Summary** — three cards: Cluster health (Healthy / Degraded /
  No quorum, plus Quorum and HA), Compute (online nodes, CPU %, RAM %) and
  Storage (online storages, total capacity, used, VM and LXC counts).
- **Proxmox Nodes** — one row per node: Node, Status, Uptime, CPU, RAM, Load avg,
  Storage, Version.

## What the values mean

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
- **A "Not refreshing — server unreachable" badge** in the page header means the
  browser cannot reach Homelab Vault itself; the page keeps showing the last
  data it loaded rather than going blank. Different from the Nodes badge above,
  which means the app is fine but the Proxmox data behind it is old.

## Clicking a row

Clicking a node row, or a service under Needs attention, opens a details panel on
the right. A service shows its URL, host, cluster, version and uptime where those
are known, plus an **Open external** action. Rows the app cannot fill are left
out rather than shown as a dash.

A node offers **Open web UI**, which opens that node's own Proxmox console at
`https://<node address>:8006`. If Proxmox did not report an address for a node,
the action is left out rather than pointing at a dead link.

If a service's URL is not `http://` or `https://` it is shown but not made
clickable, and no Open external action appears.

## On a phone or tablet

Below desktop width the details panel slides in from the right as an overlay
instead of sitting beside the table; **✕** closes it. Nothing behind it is
blocked — the page stays scrollable while it is open.

On a phone each node row becomes a card: the node's name and online/offline
pill first, then labelled facts — **up** (uptime), **cpu** and **ram** with
their meter bars full width, **load**, **disk** and **pve** (version).

The three summary cards (Cluster health, Compute, Storage) stack vertically on
narrower screens, and the status summary tiles wrap to fit — they remain
tappable filters.

## When Proxmox is not set up

With no Proxmox credentials in `.env`, the Proxmox panels do not appear at all
and the dashboard shows the Status Summary only. If Proxmox is configured but
polling is switched off in Settings, the panel appears and says so, rather than
showing numbers that have quietly stopped moving.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "Waiting for the first poll." | Configured, but the worker has not completed a cycle yet — wait one poll interval. |
| "Proxmox polling is switched off in Settings." | Turn on **Settings → Proxmox → Proxmox polling**. |
| "Cluster state is unavailable — see Settings for the reason." | The poll failed. **Settings → Proxmox** shows the actual reason, which is kept off this page deliberately because the page is public. |
| No Proxmox panels at all | `PROXMOX_HOST` and `PROXMOX_API_TOKEN` are not set in `.env`. |
| A node row with dashes in Load avg and Version | That node did not answer its per-node status call. The rest of its row is still accurate. |
| The dashboard is empty except for Status Summary | Either Proxmox is not configured, or both Proxmox panels are switched off in **Settings → Panels**. |

## Related

- [Settings User Guide](SETTINGS_USER_GUIDE.md)
- [Service Catalog User Guide](CATALOG_USER_GUIDE.md)
