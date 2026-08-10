# Service Catalog User Guide

**Last Updated**: 2026-08-10

## What it is

The catalog at `/catalog` is a searchable launcher: find a service, see if it is
alive, open it. It is readable by anyone who can reach the app — no login — and
that is deliberate, so it works as a browser home page. Changing it needs the
admin token, unless the server runs an open auth mode (see the
[Settings User Guide](SETTINGS_USER_GUIDE.md)).

The whole catalog loads into your browser once and every search filters it
there, which is why results are instant and why the table's footer says
*"filtered in the browser"*.

## Finding a service

### The search box

Bare words fuzzy-match a service's name, slug, description and tags. Prefixes
filter specific fields:

| Prefix | Matches | Example |
|---|---|---|
| `host:` | location, as a substring — both a typed location and a Proxmox-derived one | `host:pve-c` |
| `cat:` | category, as a substring | `cat:media` |
| `tag:` | a tag, exactly | `tag:backup` |
| `state:` | status, exactly: `up` `down` `stale` `unknown` `off` | `state:down` |
| `src:` | how the record got here: `manual` `api` `discovered` `manifest` | `src:manual` |

Terms AND together, with three refinements:

- **Prefix any value with `!` to negate it** — `state:!up` is everything not up.
- **Repeating `state:`, `cat:` or `src:` matches either** (`state:down state:stale`).
  A service has exactly one status, so requiring two at once would match
  nothing.
- **Repeating `tag:` or `host:` requires all of them** — a service can carry
  many tags, so asking for two is a real question.

Multi-word values need quotes — `cat:"home automation"` — and clicking a chip
quotes them for you. **Esc clears the box.** The **Search syntax** toggle under
the bar shows a clickable reference of all of this.

Everything currently filtering is shown as a row of removable chips with a
**Clear all** link, so a puzzling result set is always explainable. A **Tags**
row beneath offers up to ten tags you have not filtered by yet.

### The command palette

**⌘K** (or **Ctrl+K**) opens the palette from any screen. Type to match
services by name, slug or category — saved views appear too. **↑ ↓** move,
**↵** opens the service's record, **⌘↵** (or **Ctrl+↵**) launches it, **Esc**
closes. It shows at most 8 services and 3 views, so keep typing to narrow.

### Status counts as filters

The counts in the page header — up · down · stale · unknown · no check — are
buttons. Clicking one toggles that status filter (the *no check* count writes
`state:off`, its filter name).

### Saved views

A saved view is a stored query string. Nothing more — no snapshot of results,
no settings of its own.

- **Create** with the **＋** beside *Saved views* in the sidebar, or the
  **Save view** chip in the search bar. You name it; the query is shown
  read-only, because you are saving what you are looking at.
- **Apply** by clicking it in the sidebar or the *Views* chip row. Each view
  shows a live count of matching services.
- **Rename or delete** with the **✎** on its row. Renaming changes only the
  label, never the query — a saved view *is* its query, so to change what it
  finds, save a new view. Renaming onto an existing name is **refused, not
  merged**: two views are two different stored queries, and merging would
  discard one.

**One thing to watch:** saving a *new* view under a name that already exists
quietly replaces that view's query. Pick a fresh name unless replacing is what
you want.

Views appear in the order they were created; there is no reordering.

## The table

Columns: checkbox · **Service** · **Category** · **Status** · **Location** ·
**Tags** · **Launch**, sorted by name. Sorting is not configurable.

Most cells do something:

- **Service** — click the name or icon to open its record. A service with no
  icon shows two-letter initials instead.
- **Category** — a chip that filters by that category. Category clicks are
  single-select: clicking a second one replaces the first, and clicking the
  active one clears it.
- **Status** — clicking the pill **re-checks the service right now** (the
  tooltip says *Check now*). It does nothing on a service whose checks are
  switched off.
- **Location** — your typed value if you set one, otherwise the
  Proxmox-derived one, otherwise a dash. A small amber **stale** tag can appear
  on a derived value — see [Location](#location-yours-vs-proxmoxs).
- **Tags** — each tag adds a `tag:` filter, and tag filters accumulate.
- **Open ↗** — launches the service, always in a new tab. Ctrl/⌘-click does
  the same.

Row height, icon and text size follow **Settings → Appearance → Catalog
density** (Compact or Comfortable), which is server-wide — every browser sees
the same catalog.

## What the statuses mean

| Status | Meaning | What to do |
|---|---|---|
| **Up** (green) | The last check succeeded. The note says how recently. | Nothing. |
| **Down** (red) | The service failed its fail threshold of checks **in a row** — 3 by default. One failure never flips it. The note says how long it has been down. | Look at the service. |
| **Stale** (amber, hollow dot) | The last check is older than three check intervals — **the worker stopped checking; it says nothing about the service.** The note reads *worker not reporting*. | Check the worker line at the foot of the sidebar. |
| **Unknown** (grey) | Never checked yet. | Wait one interval, or click the pill to check now. |
| **No check** (grey) | Health checks are switched off for this service. A choice, not a fault — it still lists and launches. | Nothing, unless you want it checked. |

Stale is deliberately amber with a hollow dot so it does not read as another
kind of down — it is a different problem, in your monitoring rather than your
service.

Four behaviours worth knowing:

- **What counts as a failure** is connection refused, DNS failure, or a
  timeout. **Any HTTP response counts as success** — a service serving an
  error page shows *Up*, because up means *reachable*.
- **Recovery is damped.** A service coming back from down needs 2 successes in
  a row (by default) before it shows *Up* again, so a flapping service does not
  churn.
- **A first check goes straight to up**, silently. There is nothing to damp on
  a service that was never down.
- **Stale and no-check are worked out live in your browser**, so a dead worker
  surfaces on a page you already have open — no reload needed.

## Adding a service

**＋ Add service**, top right (or **＋ Add your first service** on an empty
install). The modal has three tabs: **Fast path**, **More options**, **Bulk
import**.

### The fast path

**Launch URL is the only required field.** Paste it and press Enter: the server
probes the URL and fills in the name, slug and icon for you — never overwriting
anything you already typed — and shows what it found: whether the page
answered, its title, the final URL, and the Proxmox-derived location if there
is one. Probing needs the admin token; you can still save without it, and a
failed probe never blocks saving.

The **slug** is the service's permanent name — it cannot be changed after
saving, so prefer what the service *is* over where it currently lives.
**Save service** stays disabled until both the URL and the slug are filled.

Two rules you may run into:

- URLs must be full `http://` or `https://`, scheme included. Anything else is
  refused with a message naming the field.
- A URL that is an IP address gives the probe nothing to derive a name from,
  so you supply the slug and display name yourself — the form says so when it
  happens.

Display name, category and icon are all optional. As the form puts it: *a
service with only a name and URL is a complete, valid record.*

### More options

- **Location** — leave empty to follow Proxmox; typing a value pins it. See
  [Location](#location-yours-vs-proxmoxs).
- **Tags** and **Description**.
- **Health URL** — where the checker should look, if that differs from the
  launch URL. It is a complete URL, not a base: your browser may need
  `https://name.lab.local` while the worker needs `http://192.168.0.101:3000`.
  A `tcp://host:port` value switches that service to a plain TCP port check.
- **Health checks** — a checkbox. Off means the service still lists and
  launches; it just shows no status.

### Bulk import

Paste **one URL per line**, and optionally a category and tags for the whole
batch — everything in one batch gets the same ones, so import things that
belong together; one batch per category is the easy way to work.

**Probe and review →** probes every line and shows a review table: each row
gets a derived slug and name (both editable), a reachability dot, and a drop
button. Rows are flagged with **Needs a slug**, **Duplicate slug in this batch
— only the last one would survive**, or **Already in the catalog — will update
it**, and the import button stays disabled until duplicates are resolved.
**Import N service(s)** then does the work; successful rows leave the list and
problem rows stay for another pass.

## Editing and deleting a service

Every service has its own page at `/services/<slug>` — a real URL you can
bookmark. From there:

- **Edit** opens the full form: name, category, launch and health URLs,
  location, tags, description, icon, and the health fields below. There is no
  slug field, because the slug is permanent — if it truly must change, delete
  the service and re-create it.
- **Delete** confirms first, and if you last entered the admin token more than
  five minutes ago it asks for it again. That is a confirmation, not a
  sign-out — adding and editing carry on as normal.

## Health checks on a service

**Poll interval** and **Fail threshold** on a service are overrides. Leave one
blank and the service follows the value on the Settings screen — the
placeholder tells you what that currently is. Type a number and this service
keeps it whatever Settings says (the interval has a 15-second floor).

Two switches that are easy to conflate:

- **Health checks enabled** off — the service stops being checked. Its status
  becomes *No check* and the recheck controls go inert.
- **Notify on status changes** off — the service is still checked on schedule
  and still shows its real status; it just never posts to Discord.

**Recheck** — the ↻ button on the record, the status pill in the table, or the
bulk bar — runs the check immediately but plays by the same rules as the
worker: a single failed recheck counts one failure toward the threshold and
will not flip a service straight to down.

## Location: yours vs. Proxmox's

The Location field is yours to type, and Proxmox's to derive. They never
overwrite each other.

- **Empty** — it follows Proxmox. The record shows the derived value with a
  *from proxmox* tag, and if the guest moves to another node the location
  follows on the next poll.
- **Typed** — it pins, shown with a *pinned* tag. Proxmox will never overwrite
  it.
- If a pinned value later disagrees with what Proxmox reports, the record
  shows both with one button: **Unpin and follow it**. There is deliberately
  no "accept" button — accepting would copy today's answer and pin it again,
  and you would be accepting forever. If you want the value to track, unpin
  it; if you want it frozen, type it.

A small amber **stale** tag beside a *derived* location means Proxmox has not
reported that guest for several polls. If the guest is really gone, nothing to
do: the link drops on its own after enough polls and the location empties. The
tag never appears on a pinned value — a pin's accuracy is yours, not
Proxmox's, to judge.

Two limits, stated on the form as well: containers running Docker resolve to
their **host**, not to themselves, and VMs need the QEMU guest agent running to
report an address. For why a service has no location at all, see the Proxmox
section of the [Settings User Guide](SETTINGS_USER_GUIDE.md).

## Icons

Most services need nothing: the probe finds the site's own icon when it can.
When it cannot, or you want a different one, the Icon field offers three
routes:

- **Fetch** — paste an image URL and press it.
- **↻ Auto** — retry discovery against the service's page. This appears on the
  edit form only.
- **Upload…** — a local PNG, SVG, ICO, JPEG, GIF or WEBP up to 512KB. The file
  is checked by its content, not its name.

Whichever route, the image is copied and served by Homelab Vault itself — the
original URL is not kept, so an icon never breaks because its source moved.
**✕** removes an icon; a service without one shows two-letter initials.

Icon actions need the admin token in every auth mode, because they make the
server fetch and store bytes on your say-so.

## Managing categories and tags

Categories and tags come from services — there is no separate admin screen for
them. The first category you type becomes a suggestion for the next service.

The **✎** beside a category (sidebar) or a tag (the search bar's Tags row)
opens an edit modal:

- **Rename** updates every service carrying it. Renaming onto an existing name
  **merges** them into one — the modal warns first. That is deliberate and the
  opposite of the saved-view rule: merging two spellings of one category is
  the point.
- **Remove from all** strips it from every service. The services themselves
  are not deleted, and the confirmation says so.

## Bulk actions

Tick row checkboxes and a bar appears: *N selected*, **＋ Add tags**,
**Set category**, **↻ Recheck**, **Clear selection**.

- **Add tags** merges into each service's existing tags — nothing is replaced.
- **Set category** replaces the category on everything selected; leaving it
  blank clears it.
- **Recheck** re-checks the lot; services with checks switched off are simply
  skipped.

Bulk edits deliberately do not re-prompt for the admin token: they are
recoverable, and a prompt that fires on routine work is one you learn to click
through without reading.

## When it asks for a token

The first time a browser tries to change anything, an **Admin token required**
prompt appears. Paste the admin token from your `.env`; it is exchanged for a
session cookie and the browser stays signed in for **14 days**.

Deleting a service, a saved view, a tag or a category asks again if the token
was last entered more than five minutes ago — a confirmation, not a sign-out.
In an open auth mode some writes need no token at all; the
[Settings User Guide](SETTINGS_USER_GUIDE.md) covers the modes and their
trade-offs.

## How the page stays fresh

The catalog refetches every 15 seconds while its tab is visible, pauses while
the tab is hidden, and catches up the moment you return. If the server stops
answering, the page keeps showing the last data it loaded rather than going
blank, and the sidebar says **Not refreshing — server unreachable**.

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| Everything went **Stale** at once | The health worker stopped — stale measures check age, not your services | Check the worker line at the sidebar's foot; restart the worker |
| **Down**, but it opens fine in your browser | The worker checks from the server, which may not reach the name or address your browser does | Set an explicit **Health URL** the worker can reach |
| **Up**, but the page is broken | Up means *reachable* — any HTTP response counts, error pages included | Nothing to fix in the catalog; look at the service |
| *"Must be a full http:// or https:// URL"* | The URL is missing its scheme | Add `http://` or `https://` |
| The form demands a slug for an IP URL | An IP address gives nothing to derive a permanent name from | Type a slug and display name yourself |
| No way to edit the slug | Slugs are permanent — they are the service's identity | If it truly must change, delete and re-create |
| Clicking **Open ↗** does nothing | The launch URL is not `http`/`https`, so the app refuses to open it | Edit the service and fix the Launch URL |
| Adding a service updated an existing one instead | Same slug — same service. Registering an existing slug updates it | Expected; use a different slug for a genuinely new service |
| *"Catalog limit of 2000 services reached"* | The catalog refuses new records at its ceiling; updates still work | Delete services you no longer need |
| A **stale** tag on a location, but the header says 0 stale | Two different things share the word: the header counts *health* staleness, the location tag is Proxmox not reporting that guest | Read each where it appears; see [Location](#location-yours-vs-proxmoxs) |
| Clicking a status pill does nothing | Health checks are switched off for that service | Turn **Health checks enabled** on if you want it checked |
| A saved view's results changed on their own | Saving a new view under the same name replaced its query | Recreate it; use fresh names for new views |

## Related

- [Settings User Guide](SETTINGS_USER_GUIDE.md) — auth modes, global health
  thresholds, Proxmox setup and location troubleshooting, catalog density
- [Command Center User Guide](DASHBOARD_USER_GUIDE.md) — the dashboard's
  status summary and Proxmox panels
