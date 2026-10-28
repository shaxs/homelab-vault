# Settings and Access User Guide

**Last Updated**: 2026-10-28

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

## Finding your way around

Settings shows **one section at a time**. A list on the left — **Appearance,
Panels, Health checks, Notifications, Proxmox, TrueNAS, Access, Data and API**
— picks which one; on a phone the same list is a row of chips above the
content that scrolls sideways. Each section has its own address
(`/settings/notifications`, `/settings/truenas`, …), so a bookmark or a link
lands on the right section after a reload.

Switching sections never loses an edit: unsaved changes stay put, the save bar
stays visible, and any section holding an unsaved edit shows a small amber dot
in the list so you can find your way back to it.

## What you can change here, and what you cannot

Not every row on this screen is editable, and each card tells you what it
holds with a small chip beside its title:

- **Editable** — every value on the card can be changed here, and takes effect
  without a restart.
- **Read-only** — every value on the card comes from `.env`. The card shows
  what is set, but changing anything means editing `.env` and restarting the
  server. There is deliberately nothing to click.
- **Mixed** — both kinds on one card. The TrueNAS card is the usual example:
  the polling toggle and interval save from here, while the host, key and
  username sit below in recessed grey blocks marked **MANAGED IN .ENV**.

Hovering a chip shows the same explanation in place. The recessed grey blocks
are the `.env` half everywhere they appear — variable name, a status instead of
a value, no control.

For the rows you *can* edit, editing works like a form, not like a light
switch:

1. Change what you want. Each changed row grows a small **changed** badge.
2. A bar appears at the bottom of the screen — *"2 unsaved changes · Poll
   interval · Discord notifications"* — and any section holding an unsaved
   change shows a small amber dot in the section list.
3. Press **Save changes** to commit, or **Discard** to put everything back.

**Nothing is saved until you press Save changes.** Switching sections keeps
your edits; leaving the Settings screen entirely with the bar still showing
throws them away.

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
[Command Center User Guide](COMMAND_CENTER_USER_GUIDE.md).

**Catalog density** — how large the catalog's rows are. **Compact** is the
original dense table; **Comfortable** makes rows taller with bigger icons and
text. Server-wide like the landing page (every browser sees the same catalog),
not per-device like Theme.

### Panels

Which panels appear on the dashboard. Switching one off removes it from the page
entirely — it is not merely hidden, so a panel you have switched off sends
nothing to the browser.

There are two Proxmox panels (**Cluster Summary**, **Nodes**) and four TrueNAS
panels (**System**, **Pools**, **Disks**, **Alerts**). The Proxmox ones are
greyed out until Proxmox is configured — your preference is still remembered
and comes back on its own once it is. The TrueNAS ones can be set at any time
and take effect once TrueNAS is configured.

> **The dashboard is readable without signing in**, like the catalog.
> Deliberate, so the dashboard is complete without a login; it assumes the app
> is reachable only from your own network. What each provider shows publicly,
> and what it keeps back, is in the [Proxmox](PROXMOX_USER_GUIDE.md#who-can-see-what)
> and [TrueNAS](TRUENAS_USER_GUIDE.md#who-can-see-what) guides.

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
| **TrueNAS raises a critical or high alert** of its own (a failing disk, a degraded pool) | forwarded once as it appears; announced once when it clears |

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

Once polling is on, the card shows the **provider status** — what the last poll
actually found, which privileges the token holds, and how many shared addresses
were skipped when matching locations. If polling keeps failing, Discord hears
about it after three failed polls in a row; this card is where the real reason
lives.

Setting up the token, reading the status, and why a service might have no
location are all in the [Proxmox User Guide](PROXMOX_USER_GUIDE.md).

### TrueNAS

**TrueNAS polling** on/off, and a **poll interval** (default 120s, minimum 30s).
Polling is **off by default**; turning it on takes effect on the TrueNAS worker's
next cycle, with nothing to restart — the same shape as the Proxmox card, and a
*separate* worker, so one provider being slow or down never affects the other.

Below the polling controls sit three `.env` values — **`TRUENAS_HOST`**,
**`TRUENAS_API_USER`** and **`TRUENAS_API_KEY`** — each showing only whether it
is set. If some are set and some are not, the provider row names the missing
variable rather than a bare "Not configured".

> **TrueNAS revokes any API key it sees over plain HTTP.** The app always
> connects over TLS, but never test the key by hand with anything unencrypted.

Creating the read-only service account, what the provider row's statuses mean,
and which TrueNAS alerts are forwarded to Discord are all in the
[TrueNAS User Guide](TRUENAS_USER_GUIDE.md).

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
| Discord is on but nothing arrives | `HOMELABVAULT_DISCORD_WEBHOOK_URL` is unset, or does not start `https://`, or the server was not restarted after setting it | Check the Notifications card says **Configured**, then check the server log — it names the variable when the value is wrong |
| A service went down and said nothing | Either its own **Notify on status changes** is off, or it had not failed the fail threshold yet | Check the switch on the service record; the count resets on any success |
| Discord announced a stall right after a restart | The worker had not beaten yet when the check ran | It clears itself on the next cycle. If it does not, the worker is not running |
| I configured a mention and nobody was pinged | The ID is wrong, or a user ID was pasted into the role field (or vice versa) | Copy the ID again with Developer Mode; users go in **Mention user ID**, roles in **Mention role ID** |
| A service named `@everyone` did not ping everyone | Working as intended — text can never cause a ping; only the configured IDs can | Nothing to fix |
| The Proxmox or TrueNAS card says **Not working**, or a location looks wrong | A provider problem rather than a Settings one | See Troubleshooting in the [Proxmox](PROXMOX_USER_GUIDE.md#troubleshooting) or [TrueNAS](TRUENAS_USER_GUIDE.md#troubleshooting) guide |
