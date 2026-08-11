# Settings and Access User Guide

**Last Updated**: 2026-08-10

## Overview

The Settings screen is where you change how Homelab Vault *behaves* — which
panels show, how often services are checked, whether Discord is on. It is
deliberately **not** where you change how Homelab Vault is *secured*. Tokens,
auth mode, session length and the probe's allowed networks all live in `.env`
and need a server restart, because a control that decides whether logins matter
should not be reachable by someone who is logged in.

The screen shows you both halves. Anything you can edit sits on a normal row
with a real control; anything owned by `.env` sits in a recessed grey block with
its variable name, a status instead of a value, and nothing to click.

## Getting to Settings

Two ways:

- click **Settings** at the foot of the sidebar, below the rule, or
- click the **auth: _mode_** chip in the top bar.

You need the admin token. If this browser has not been signed in, the token
prompt appears — the same one you get the first time you edit the catalog. Once
you have entered it, the browser stays signed in for **14 days** and you will
not be asked again just to open Settings.

**Settings is admin-only in every auth mode**, including `open`. That is
deliberate: the screen lists which credentials are configured and can switch
notifications off, and neither belongs in front of an anonymous visitor.

## What you can change here, and what you cannot

Editing works like a form, not like a light switch:

1. Change whatever you want. Each changed row grows a small **changed** badge.
2. A bar appears at the bottom of the screen — *"2 unsaved changes · Poll
   interval · Discord notifications"*.
3. Press **Save changes** to commit, or **Discard** to put everything back.

**Nothing is saved until you press Save changes.** Leaving the screen with the
bar showing throws the edits away.

One row does not work this way: **Theme** applies the instant you click it and
is never part of the save bar, because it is stored in your browser rather than
on the server.

## The cards

### Appearance

**Theme** — Light or Dark, marked `THIS DEVICE`. It lives in this browser only.
It is not saved to the server, does not follow you to another machine, and
nobody else sees your choice. Deliberate: it is a display preference, not
catalog data. On a phone the top bar hides its Light/Dark toggle, so this row
is where you change theme there. Settings rows stack on narrow screens — the
control drops below its label — but behave identically.

**Landing page** — what loads when you visit the root of the site: **Service
Catalog** or **Command Center**. Unlike Theme, this one is server-wide. Either
way the catalog stays reachable at `/catalog`, so a bookmark to it keeps working
whatever this is set to. See the
[Command Center User Guide](DASHBOARD_USER_GUIDE.md).

**Catalog density** — how large the catalog's rows are. **Compact** is the
original dense table; **Comfortable** makes rows taller with bigger icons and
text. Server-wide like the landing page (every browser sees the same catalog),
not per-device like Theme.

### Panels

Which panels appear on the dashboard. Switching one off removes it from the page
entirely — it is not merely hidden, so a panel you have switched off sends
nothing to the browser.

The two Proxmox panels are greyed out until Proxmox is configured — your
preference is still remembered and comes back on its own once it is.

> **The dashboard is readable without signing in**, like the catalog. That
> includes node names, node addresses, Proxmox versions and storage totals. Deliberate, so the
> dashboard is complete without a login; it assumes the app is reachable only
> from your own network. Your VM and container names, and the reason a Proxmox
> poll failed, stay on this screen instead.

### Data and API

Two links, both opening in a new tab and neither changing any setting:
**Export JSON** gives the whole catalog as the public API returns it (settings
are deliberately excluded), and **API docs** opens the generated OpenAPI schema,
which is this project's canonical API reference. They used to sit in the top
bar; they are occasional, deliberate actions rather than navigation, so they
live on the screen you visit on purpose.

### Health checks

These apply to **every service that has not been given its own interval on its
own record**. A service with a hand-set interval keeps it; clear that field on
the service and it follows the value here again.

| Setting | Default | Minimum | What it does |
|---|---|---|---|
| Poll interval | 60s | **15s** | How often the worker checks each service |
| Fail threshold | 3 | 1 | Failures in a row before a service shows as **down** |
| Recovery threshold | 2 | 1 | Successes in a row before it goes back to **up** |

**Worker stall alert** is a fourth row you cannot edit. It shows three times the
poll interval — the silence after which Homelab Vault decides the health worker
has stopped — and moves on its own when you change the interval above it.

> **A service with no interval of its own now genuinely has none.** Before this
> release every service was stamped with a copy of the interval when it was
> created, so "applies to every service without its own interval" described a
> state nothing could be in. Clearing the field on a service record leaves it
> blank, and blank means *follow this card*.

### Notifications

A **Discord notifications** on/off toggle, off by default. The webhook URL itself
is not here: it lives in `.env` as `HOMELABVAULT_DISCORD_WEBHOOK_URL`, and the
screen only tells you whether it is configured. The URL is never displayed and
never returned by the API, so a screenshot of this page cannot leak it. Change it
by editing `.env` and restarting.

**Webhook variable** shows *which* environment variable is consulted —
`HOMELABVAULT_DISCORD_WEBHOOK_URL` — and is **display-only**. There is one
webhook and one variable to hold it, so a field that could only ever be set one
correct way would be a field that could only ever be set wrong: point it
somewhere else and notifications stop, quietly. What it is for is answering
*"which variable do I edit?"* without you having to go and look.

Whatever it names must hold a value starting `https://`. If it does not, nothing
is sent and the server log says which variable is at fault, without printing
what is in it.

#### What a message looks like

Every alert is a Discord **embed** — a card with titled fields — plus one plain
line of text above it (that line is what a phone's push preview shows). The
fields are fixed: **Event** (went down / recovered / worker stalled / provider
poll failing), **Was down for** on a recovery, the service's **URL**,
**Category** and **Tags**, with the slug in the footer and a timestamp. Fixed
on purpose: an agent watching the channel can parse the same fields every time,
which is the point of the format. There is no message template to edit.

Only an `http://` or `https://` launch URL appears in the URL field — anything
else is left out, the same rule the catalog itself applies before linking to a
service.

#### Mentioning someone (or your agent)

**Mention user ID** and **Mention role ID** — a Discord *ID* to ping on every
notification. Empty (the default) pings nobody. Set one or both; the ping is
prepended to every message the moment you save.

IDs only, never names, and that is a security rule rather than a convenience:
a service's name can come from a probed page title, so text is never allowed to
cause a ping — a service literally named `@everyone` posts as inert text. Your
configured ID pings because it was named explicitly in the message's allowlist,
not because Discord was told to parse the text.

To find an ID: Discord → Settings → Advanced → enable **Developer Mode**, then
right-click a user or role → **Copy ID**. It is a 17–20 digit number; the field
accepts nothing else.

**Sends a message when** lists what will actually fire, worked out from the
thresholds on the Health checks card rather than written down — so it changes
when you change them:

| Trigger | Message |
|---|---|
| A service fails *Fail threshold* checks in a row | announced as down |
| It then passes *Recovery threshold* checks in a row | announced as recovered |
| A worker goes quiet for three poll intervals | announced as stalled, then again when it comes back |
| A Proxmox poll fails **3 times in a row** | announced as failing, once; announced again when it recovers |

**Recovery is damped on purpose.** A service that flaps up and down would
otherwise post on every bounce, and what people do about a channel like that is
mute it — which mutes the alerts that matter too.

**A service being checked for the first time is not announced.** It goes to up
and says nothing, because "is back up" is untrue of something that was never
down, and a bulk import would otherwise post once per service.

**Silencing one service** is on the service's own record: **Notify on status
changes**. Turn it off and the service is still checked on the same schedule and
still shows its real status on the dashboard — it just never posts. That is a
different switch from **Health checks enabled**, which stops checking it at all.

### Worker health

If the health worker dies, nothing else notices on its own: every status freezes
at whatever it last was — almost always **up** — and no alert fires, because
alerts fire on *changes* and there are no changes without checks. The dashboard
would show a healthy lab all night.

So the absence of a signal is treated as the signal. Each worker records a
heartbeat every cycle, and going quiet for three poll intervals is a stall.
Workers are tracked separately: one stopping does not hide another still running.

`GET /api/v1/healthz` reports this for something outside Homelab Vault to watch.
It answers **503** while any worker is stalled and **200** otherwise, so a monitor
can alert on the status code alone. Anonymous callers see each worker's name,
state and last heartbeat; the numbers behind it need the admin token.

Polling it also *runs* the check, so whatever you point at `/healthz` drives the
internal alert as well as watching for the site being down. Alerts are sent once
when a worker stalls and once when it recovers — not once per poll.

> **The one thing this cannot catch is the whole container being down.** Nothing
> inside the app can report that. Point an external monitor at `/healthz` if it
> matters to you, which for uptime alerting it usually does.

### Proxmox

**Proxmox polling** on/off, and a **poll interval** (default 120s, minimum 30s —
higher than the health interval because the cluster API is heavier). Polling is
**off by default**; turning it on takes effect on the worker's next cycle, with
nothing to restart.

The whole card is greyed out until `PROXMOX_HOST` and `PROXMOX_API_TOKEN` are
set in `.env` and the server is restarted. Until then the poller stays off and
the Proxmox panels are hidden everywhere.

#### Provider status

Once polling is on, the card shows what the last poll actually found:

| It says | It means |
|---|---|
| **Working**, with a guest count | The cluster answered and the credential can see it |
| **Working**, plus *"N shared addresses skipped"* | Normal. See below — this is usually the answer to "why did some services not get a location" |
| **Not working**, *"Waiting for the first poll"* | Fresh start. Give it one poll interval |
| **Not working**, with a reason | Read the reason; the common ones are covered in [Troubleshooting](#troubleshooting) |
| **Token privileges: …** | What the credential could actually do on the last successful poll. `VM.Audit` and `VM.GuestAgent.Audit` are what locations need; `Sys.Audit` is what the dashboard's cluster panels need — if they say "unavailable" while locations work, look for it here |

> **"Not working" with a guest count of zero is a state you should never see, and
> that is deliberate.** Proxmox answers a request for guests your credential
> cannot see with an empty list rather than an error — so a token with no
> permissions looks exactly like an empty cluster. Homelab Vault checks the
> credential's permissions directly instead of trusting the empty list, and tells
> you which commands to run. Without that, the only symptom would be locations
> that never appear.

A **Cluster panel:** note appears here when the guest list works but the
*cluster* read does not (typically a token without `Sys.Audit`). That is the
reason the dashboard means when it says *"Cluster state is unavailable — see
Settings for the reason"* — guest locations keep working; only the dashboard's
cluster and node panels are affected.

**If polling keeps failing, Discord hears about it** (when notifications are
on): one message after **three consecutive failed polls**, one more when it
recovers — never one per poll. The message deliberately does not carry the
diagnostic; this card is where the real reason lives. A single failed poll is
treated as a blip and announced to nobody. The worker itself keeps running
through all of this, which is exactly why the worker-stall alert cannot cover a
broken cluster — the worker is fine; the cluster is not.

#### Why a service might have no location

A service gets a location when its address matches **exactly one** guest.

- **Containers running Docker resolve to their host.** If ten services run in
  containers on one Proxmox container, all ten show *that container's* location.
  That is genuinely where they are, and it is worth knowing that is what the
  field means.
- **An address held by more than one guest is skipped.** Every guest running
  Docker reports `172.17.0.1`, so that address identifies none of them. Homelab
  Vault would rather show nothing than show you a guess. The *"N shared addresses
  skipped"* count on the card is how many it ignored.
- **VMs need the QEMU guest agent** running to report an address. Containers do
  not. Expect partial coverage if you run VMs without it.
- **A service on hardware outside the cluster has no location and never will** —
  a NAS, a Raspberry Pi, a desktop. That is not a fault.

#### Location on a service: yours vs. Proxmox's

The **Location** field on a service record is yours to type, and Proxmox's to
derive. They never overwrite each other.

- Leave it **empty** and it follows Proxmox. If the guest moves to another node,
  the location follows it on the next poll — no action from you.
- **Type a value** and it *pins*. Proxmox will never overwrite it.
- If a pinned value later disagrees with what Proxmox reports, the record shows
  both, with **Unpin and follow it**. That clears your typed value and lets the
  location track again.

> **There is no "accept" button, on purpose.** Accepting would copy Proxmox's
> current answer into your pinned value — which pins it again, so the two
> disagree again the next time the guest moves, and you accept forever. If you
> want the value to track, unpin it. If you want it frozen, type it. Those are
> the only two things anyone actually wants.

### Access

Entirely read-only — every value on this card comes from `.env`:

- the active **auth mode**, shown as a framed group of three with a radio dot —
  exactly one is ever filled, because the modes are mutually exclusive. The
  dots are indicators, not buttons: the caption on the frame names the variable
  to edit
- **session lifetime** (14 days, from `SESSION_COOKIE_AGE`)
- whether the **admin** and **agent** tokens are configured — configured or not,
  never the values
- the **probe allowed networks**

Changing any of them means editing `.env` and restarting the server. There is no
privilege level in the UI that can do it.

## Auth modes

Set with `HOMELABVAULT_AUTH_MODE` in `.env`. A restart is required.

| Mode | What it means |
|---|---|
| **`full`** | The default. A token is required for every write. |
| **`open-writes`** | No token to add or edit a service. Deleting still needs the admin token. |
| **`open`** | No token for anything, except probe, icon and Settings, which always need admin. |

`open-writes` is the one most homelabs actually want: adding a service takes
fifteen seconds with no token, while a forgotten session on a lost phone still
cannot strip a tag from every service in one misclick.

If the mode is anything other than `full`, a warning banner stays on the Settings
screen and the top-bar chip turns amber. You cannot switch the mode back from
the screen — the banner names the variable to edit, because the only way to fix
it is at the shell.

> ### ⚠ The real risk of an open mode is not vandalism
>
> Adding a service makes **the server fetch the URL you gave it**. In an open
> mode, anyone who can reach Homelab Vault can therefore make it fetch things
> *inside your network* — a device's admin page, a service with no auth, an
> address that only exists behind your firewall. The catalog getting messy is
> the least of it.
>
> Only run an open mode on a network you fully trust, and keep
> `PROBE_ALLOWED_NETWORKS` as tight as you can. Probe and icon stay admin-only
> in every mode, which narrows this — it does not close it.

## When it asks for your token again

Deleting a **service**, a **saved view**, a **tag** or a **category** asks for
the admin token again if you last entered it more than **5 minutes** ago. You
stay signed in — it is a confirmation, not a sign-out — and the prompt says so.

**Bulk edits deliberately do not ask.** Adding tags to twenty services, or
setting a category on all of them, goes straight through. Those are recoverable,
and a prompt that fires on routine work is one you learn to click through
without reading, which protects nothing.

Change the five minutes with `ADMIN_REAUTH_WINDOW_S` in `.env`.

**If you rotate `HOMELABVAULT_ADMIN_TOKEN`,** every signed-in browser is signed
out the next time it does anything, and has to enter the new token. That is the
point of rotating it.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| Settings asks for the token every single time | The prompt was cancelled, or the browser is blocking cookies for this site | Enter the token and let it complete; check that cookies are allowed. The session cookie is what remembers you |
| It asks again when I delete something, but I *am* signed in | Working as intended — deletes re-confirm after 5 minutes | Enter the token again, or raise `ADMIN_REAUTH_WINDOW_S` in `.env` |
| *"has a minimum of 15 seconds"* when saving | The floor is enforced by the server, not only by the stepper. Something sent a lower value — usually a script rather than the screen | Use 15 or higher. The stepper on the screen already stops there |
| The Proxmox card is greyed out | `PROXMOX_HOST` / `PROXMOX_API_TOKEN` are not set, or the server has not been restarted since they were | Set both in `.env`, restart, reload the page |
| My theme did not follow me to another computer | By design — theme is per-browser and never saved to the server | Set it once on each device |
| The auth chip is amber, not purple | You are in `open-writes` or `open` | If that was not intended, set `HOMELABVAULT_AUTH_MODE=full` in `.env` and restart |
| I was signed out with no warning | The admin token was rotated and the server restarted | Enter the new token |
| A setting saved, but nothing changed | Every setting on this screen takes effect on the relevant worker's **next cycle** — nothing needs restarting | Wait one interval. Health settings follow the health interval, Proxmox settings the Proxmox interval |
| Provider status says **Not working**, *"authenticates but holds no privileges"* | The API token is real but was never granted a role. With privilege separation, a token's rights are the **intersection** of the user's and the token's, so granting only one leaves it blind | Run **both** lines the message gives you — one `--users`, one `--tokens` — then `pveum user token permissions <user> <token>` to confirm |
| Provider status says **Not working**, *"must include the scheme"* | `PROXMOX_HOST` is missing `https://`. The Proxmox API is HTTPS on port 8006 | Set `PROXMOX_HOST=https://your-node:8006` and restart |
| Provider status says **Not working**, a 401 | `PROXMOX_API_TOKEN` is only the secret UUID. Proxmox identifies a token by `USER@REALM!TOKENID` and authenticates it with the UUID, so the UUID alone cannot be matched to a token | Use the complete `PVEAPIToken=USER@REALM!TOKENID=UUID` string, then restart |
| Services on one host all show the same location | Working as intended — containers running Docker resolve to their **host** | Nothing to fix. Type a Location on a service if you want it to say something else |
| A service that should match has no location | Its address is shared by more than one guest, or it is a VM without the QEMU guest agent, or it is not on the cluster at all | Check the *"N shared addresses skipped"* count on the Proxmox card first — that is the usual cause |
| A location is stuck on an old node after moving a guest | Somebody typed into the Location field, which **pins** it | Open the service record; if it shows a disagreement, click **Unpin and follow it** |
| A location shows a small **stale** tag | Proxmox has not reported that guest for several polls — usually it was deleted, or the whole poll is failing | If the guest is really gone, nothing to do: the link drops on its own after enough polls and the location empties. If the guest exists, check this card's status |
| The server log warns about `PROXMOX_VERIFY_TLS` | Expected on a default Proxmox install, which uses a self-signed certificate. The warning exists because the API token crosses that connection | Either accept it on a trusted network, or give the cluster a certificate your machine trusts and set `PROXMOX_VERIFY_TLS=True` |
| Discord is on but nothing arrives | `HOMELABVAULT_DISCORD_WEBHOOK_URL` is unset, or does not start `https://`, or the server was not restarted after setting it | Check the Notifications card says **Configured**, then check the server log — it names the variable when the value is wrong |
| A service went down and said nothing | Either its own **Notify on status changes** is off, or it had not failed the fail threshold yet | Check the switch on the service record; the count resets on any success |
| Discord announced a stall right after a restart | The worker had not beaten yet when the check ran | It clears itself on the next cycle. If it does not, the worker is not running |
| I configured a mention and nobody was pinged | The ID is wrong, or a user ID was pasted into the role field (or vice versa) | Copy the ID again with Developer Mode; users go in **Mention user ID**, roles in **Mention role ID** |
| A service named `@everyone` did not ping everyone | Working as intended — text can never cause a ping; only the configured IDs can | Nothing to fix |
| Proxmox broke and Discord said nothing | Fewer than 3 polls have failed so far, or notifications are off, or the host was *unset* rather than broken — "not configured" is a configuration state and never alerts | Wait out 3 poll intervals; check the Notifications toggle and this card's status |
