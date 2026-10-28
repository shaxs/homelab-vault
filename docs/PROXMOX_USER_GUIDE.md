# Proxmox User Guide

**Last Updated**: 2026-10-28

## What it is

An optional, read-only connection to a **Proxmox VE** cluster. Once it is set
up, Homelab Vault:

- fills in each catalog service's **Location** — which node and guest it runs
  on — and keeps it current as guests move,
- shows the cluster and its nodes in a **Proxmox section** of the Command
  Center, and
- tells Discord when it can no longer reach the cluster.

Nothing here ever changes anything on the cluster. With Proxmox left
unconfigured, there are no locations, no Proxmox pill on the dashboard and no
polling, and the rest of the app behaves exactly the same. The catalog is the
product; location is extra.

## Setting it up

### 1. Create a read-only API token

Run these on any node, as root:

```bash
pveum user add homelabvault@pve
pveum user token add homelabvault@pve readonly --privsep 1
pveum acl modify / --users  'homelabvault@pve'          --roles PVEAuditor
pveum acl modify / --tokens 'homelabvault@pve!readonly' --roles PVEAuditor
```

Copy the token secret the second command prints — Proxmox shows it once.

> **Both `acl` lines are needed, and this is the easy one to get wrong.** With
> privilege separation, a token's rights are the *overlap* of the user's and the
> token's. Grant only one and the token logs in fine but sees nothing.
>
> Proxmox does not report that as an error: it answers with an empty list, so a
> blind token looks exactly like an empty cluster. Check with
> `pveum user token permissions homelabvault@pve readonly`. Homelab Vault checks
> too — see [Checking that it works](#checking-that-it-works).

**`PVEAuditor` is all it needs.** On Proxmox 8 and later it includes
`VM.GuestAgent.Audit`, which is what reads a VM's addresses, so the older
`VM.Monitor` privilege is not required and the token stays read-only.

### 2. Add two values to `.env`

```text
PROXMOX_HOST=https://192.168.0.101:8006
PROXMOX_API_TOKEN=PVEAPIToken=homelabvault@pve!readonly=<the secret>
```

| Variable | What it holds |
|---|---|
| `PROXMOX_HOST` | Any node's API address. **The `https://` and `:8006` are required** — a bare `host:port` fails on every call. |
| `PROXMOX_API_TOKEN` | **The complete token string**, `PVEAPIToken=USER@REALM!TOKENID=SECRET` — not just the secret. Proxmox cannot tell which token a bare secret belongs to. |
| `PROXMOX_VERIFY_TLS` | Optional, default `False`. See [The certificate warning](#the-certificate-warning). |
| `PROXMOX_TIMEOUT_S` | Optional, default `15.0` seconds per request. |

### 3. Restart and switch polling on

```bash
docker compose up -d
```

Then open **Settings → Proxmox** and turn on **Proxmox polling**. It is **off by
default**. The **poll interval** defaults to 120 seconds (minimum 30 — higher
than the health checks, because the cluster API is heavier). Both take effect on
the worker's next cycle, with nothing to restart.

Proxmox has its own worker, `proxmox-worker`, separate from the TrueNAS one, so
one provider being slow or down never affects the other.

## Checking that it works

<!-- SCREENSHOT PLACEHOLDER: docs/images/settings-proxmox.png — dark theme,
     desktop, Settings → Proxmox with a Working status and the privileges row. -->

**Settings → Proxmox** shows what the last poll actually found:

| It says | It means |
|---|---|
| **Working**, with a guest count | The cluster answered and the token can see it. |
| **Working**, plus *"N shared addresses skipped"* | Normal. Usually the answer to "why did some services not get a location" — see [Why a service might have no location](#why-a-service-might-have-no-location). |
| **Not working**, *"Waiting for the first poll"* | Fresh start. Give it one poll interval. |
| **Not working**, with a reason | Read the reason; the common ones are in [Troubleshooting](#troubleshooting). |
| **Token privileges: …** | What the token could actually do on the last successful poll. `VM.Audit` and `VM.GuestAgent.Audit` are what locations need; `Sys.Audit` is what the dashboard's cluster panels need. If the panels say "unavailable" while locations work, look here. |

> **"Not working" with a guest count of zero is a state you should never see,
> and that is deliberate.** Because a token with no permissions gets an empty
> list rather than an error, Homelab Vault checks the token's permissions
> directly instead of trusting the empty list, and tells you which commands to
> run. Without that, the only symptom would be locations that never appear.

A **Cluster panel:** note appears on the card when the guest list works but the
*cluster* read does not — typically a token without `Sys.Audit`. That is the
reason behind the dashboard's *"Cluster state is unavailable — see Settings for
the reason"*. Locations keep working; only the dashboard's cluster and node
panels are affected.

## Locations on catalog services

A service gets a location when its address matches **exactly one** guest on
the cluster. The location follows the guest: move it to another node and the
service's location follows on the next poll.

You can always type your own Location on a service instead, which pins it — the
[Service Catalog User Guide](CATALOG_USER_GUIDE.md#location-yours-vs-proxmoxs)
covers typed vs. derived locations, unpinning, and the **stale** tag.

### Why a service might have no location

- **Containers running Docker resolve to their host.** If ten services run in
  Docker on one Proxmox container, all ten show *that container's* location.
  That is genuinely where they are, and it is worth knowing that is what the
  field means.
- **An address held by more than one guest is skipped.** Every guest running
  Docker reports `172.17.0.1`, so that address identifies none of them. Homelab
  Vault would rather show nothing than guess. The *"N shared addresses skipped"*
  count on the Settings card is how many it ignored.
- **VMs need the QEMU guest agent** running to report an address. Containers do
  not. Expect partial coverage if you run VMs without it.
- **A service on hardware outside the cluster has no location and never will** —
  a NAS, a Raspberry Pi, a desktop. That is not a fault.

## What you see on the Command Center

**On the Overview** (`/dashboard`), a Proxmox tile shows nodes online and cluster
health. Clicking it opens the Proxmox section.

**The Proxmox section** (`/dashboard/proxmox`) has two panels, each of which can
be switched off in **Settings → Panels**:

- **Proxmox Cluster Summary** — three cards: Cluster health (Healthy / Degraded /
  No quorum, plus Quorum and HA), Compute (online nodes, CPU %, RAM %) and
  Storage (online storages, total capacity, used, VM and LXC counts).
- **Proxmox Nodes** — one row per node: Node, Status, Uptime, CPU, RAM, Load avg,
  Storage, Version. Clicking a node offers **Open web UI**, which opens that
  node's own console at `https://<node address>:8006`.

The rest of the screen — the adapter bar, the health dot on each pill, the phone
layout — is covered in the [Command Center User Guide](COMMAND_CENTER_USER_GUIDE.md).

### What the values mean

- **HA: Not configured** means Proxmox reported no high-availability services.
  **Unknown** means the token could not read HA status — it needs `Sys.Audit`.
  That is different from HA being off.
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
- **A "Not refreshing" badge** on the Nodes panel means the data is older than
  three poll intervals — usually the Proxmox worker has stopped.

## Alerts in Discord

With **Settings → Notifications → Discord notifications** on:

| What happened | What Discord gets |
|---|---|
| **The poll has failed 3 times in a row** | One message saying the provider is failing, and one when it recovers — never one per poll. A single failed poll is a blip and is announced to nobody. |
| **The Proxmox worker stopped** | The same stall alert every worker gets, once when it stalls and once when it comes back. |

The failure message deliberately leaves out the diagnostic; **Settings →
Proxmox** is where the real reason lives. The two alerts cover different
failures: when the cluster is unreachable the worker itself is fine, which is
why the worker-stall alert alone could never catch a broken cluster.

Unlike TrueNAS, Proxmox's own alerts are not forwarded.

## Who can see what

**The dashboard is readable by anyone who can reach Homelab Vault**, the same as
the catalog. For Proxmox that means node names, node addresses, versions, and
CPU, RAM and storage totals. That is deliberate, so the dashboard works without
signing in, and it assumes the app is reachable only from your own network.

Two things stay behind the admin token, on the Settings screen:

- the names and addresses of your **VMs and containers**, and
- the **reason** a poll failed, which can name an environment variable.

### The certificate warning

Proxmox ships a self-signed certificate on port 8006, so `PROXMOX_VERIFY_TLS`
defaults to `False` and the server log warns about it. The warning exists
because the API token travels on that connection. Accept it on a trusted
network, or give the cluster a certificate your machine trusts and set
`PROXMOX_VERIFY_TLS=True`.

## Troubleshooting

| What you see | Cause | What to do |
|---|---|---|
| The Proxmox card in Settings is greyed out | `PROXMOX_HOST` / `PROXMOX_API_TOKEN` are not set, or the server has not been restarted since | Set both in `.env`, restart, reload the page |
| **Not working**, *"authenticates but holds no privileges"* | The token is real but was never granted a role on both the user and the token | Run **both** lines the message gives you — one `--users`, one `--tokens` — then `pveum user token permissions <user> <token>` to confirm |
| **Not working**, *"must include the scheme"* | `PROXMOX_HOST` is missing `https://` | Set `PROXMOX_HOST=https://your-node:8006` and restart |
| **Not working**, a 401 | `PROXMOX_API_TOKEN` is only the secret | Use the complete `PVEAPIToken=USER@REALM!TOKENID=SECRET` string, then restart |
| *"Cluster state is unavailable — see Settings for the reason."* on the dashboard | The cluster read failed — often a token without `Sys.Audit` | Check the **Cluster panel:** note and **Token privileges** row on **Settings → Proxmox** |
| *"Proxmox polling is switched off in Settings."* | Polling is off | Turn on **Settings → Proxmox → Proxmox polling** |
| Services on one host all show the same location | Working as intended — Docker containers resolve to their **host** | Nothing to fix. Type a Location on a service if you want it to say something else |
| A service that should match has no location | Its address is shared by more than one guest, it is a VM without the QEMU guest agent, or it is not on the cluster | Check the *"N shared addresses skipped"* count on **Settings → Proxmox** first — that is the usual cause |
| A location is stuck on an old node after moving a guest | Somebody typed into the Location field, which **pins** it | Open the service record; if it shows a disagreement, click **Unpin and follow it** |
| A location shows a small **stale** tag | Proxmox has not reported that guest for several polls — usually it was deleted, or the whole poll is failing | If the guest is really gone, nothing to do: the link drops on its own. If it exists, check **Settings → Proxmox** |
| Proxmox broke and Discord said nothing | Fewer than 3 polls have failed, notifications are off, or the host was *unset* rather than broken — "not configured" never alerts | Wait three poll intervals; check the Notifications toggle and the Proxmox card's status |
| The server log warns about `PROXMOX_VERIFY_TLS` | Expected with Proxmox's default self-signed certificate | See [The certificate warning](#the-certificate-warning) |

## Related

- [Service Catalog User Guide](CATALOG_USER_GUIDE.md) — the Location field,
  pinning and the stale tag
- [Command Center User Guide](COMMAND_CENTER_USER_GUIDE.md) — the dashboard
  screen itself
- [Settings User Guide](SETTINGS_USER_GUIDE.md) — Discord notifications, panel
  switches, worker health
- [TrueNAS User Guide](TRUENAS_USER_GUIDE.md) — the other infrastructure
  provider
