"""Ninja/Pydantic schemas — spec §4c.

Every input schema sets `extra="forbid"`. Spec §4c: "Unknown fields are rejected
with a 422 naming them, never silently dropped. A caller that guesses a field
name must be told it guessed wrong." Ninja's validation error body reports the
offending key in `loc`, which is what makes it self-correctable by a model.

`provenance` and `created_by` are deliberately absent from the input schemas —
they are set by the server (§4c), so a caller that sends them gets a 422 naming
them rather than having them silently ignored.
"""

from datetime import datetime
from uuid import UUID

from ninja import Schema
from pydantic import ConfigDict, Field


class StrictSchema(Schema):
    model_config = ConfigDict(extra="forbid")


class ServiceIn(StrictSchema):
    """Create-or-update payload. Minimum valid payload is
    `{"launch_url": "https://karakeep.lab.local"}` (§4c)."""

    # The scheme constraints below are enforced at the model (`Service.clean()`,
    # one seam for every write path); the descriptions exist because the
    # OpenAPI schema is this API's canonical documentation and must advertise
    # the contract, not just 422 on it.
    launch_url: str = Field(
        description="Full http:// or https:// URL — the scheme is required."
    )
    # Supplied by Komodo-aware agents as the stack name; normalised through the
    # same derivation the UI uses, so the two cannot diverge (§4c).
    slug: str | None = None
    name: str | None = None
    launch_url_external: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    health_url: str | None = Field(
        default=None,
        description="Full http(s) URL, or tcp://host:port to select the tcp "
        "checker. Scheme required.",
    )
    category: str | None = None
    tags: list[str] | None = None
    icon_url: str | None = Field(
        default=None,
        description="An http(s) URL, a base64 data:image/… URI, or this "
        "app's own /media/icons/… cache path.",
    )
    description: str | None = None
    host_label: str | None = None
    repo_url: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    docs_url: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    launch_target: str | None = None
    health_enabled: bool | None = None
    notify_enabled: bool | None = None
    health_expect_status: int | None = None
    #: Null means "follow the global setting" — see `HealthPolicy`. Absent and
    #: explicitly null are the same thing here, which is why this field carries
    #: no separate unset sentinel.
    health_interval_s: int | None = None
    health_timeout_ms: int | None = None
    health_fail_threshold: int | None = None


class ServicePatch(StrictSchema):
    """Partial update. `slug` is absent by design — it is immutable after
    creation (§4); deliberate slug changes are deferred to v0.2."""

    # Scheme constraints as on `ServiceIn` — enforced in `Service.clean()`,
    # described here because the OpenAPI schema is the canonical API doc.
    launch_url: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    name: str | None = None
    launch_url_external: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    health_url: str | None = Field(
        default=None,
        description="Full http(s) URL, or tcp://host:port to select the tcp "
        "checker. Scheme required.",
    )
    category: str | None = None
    tags: list[str] | None = None
    icon_url: str | None = Field(
        default=None,
        description="An http(s) URL, a base64 data:image/… URI, or this "
        "app's own /media/icons/… cache path.",
    )
    description: str | None = None
    host_label: str | None = None
    repo_url: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    docs_url: str | None = Field(
        default=None, description="Full http(s) URL, scheme required."
    )
    launch_target: str | None = None
    ignored: bool | None = None
    health_enabled: bool | None = None
    notify_enabled: bool | None = None
    health_expect_status: int | None = None
    #: Null means "follow the global setting" — see `HealthPolicy`. Absent and
    #: explicitly null are the same thing here, which is why this field carries
    #: no separate unset sentinel.
    health_interval_s: int | None = None
    health_timeout_ms: int | None = None
    health_fail_threshold: int | None = None


class ServiceOut(Schema):
    id: UUID
    slug: str
    name: str
    launch_url: str
    launch_url_external: str | None
    health_url: str | None
    category: str | None
    tags: list[str]
    icon_url: str | None
    description: str | None
    host_label: str | None
    provenance: str
    created_by: str | None
    ignored: bool
    repo_url: str | None
    docs_url: str | None
    launch_target: str
    health_enabled: bool
    notify_enabled: bool
    health_expect_status: int | None
    #: Null means "follows the global setting". The UI needs the raw value to
    #: tell pinned from following — `health_interval_effective_s` is the number
    #: actually in force.
    health_interval_s: int | None
    health_timeout_ms: int
    health_fail_threshold: int | None
    #: Derived: the service's own interval, or the global one. The catalog
    #: re-derives `stale` client-side (`frontend/src/status.js`) and needs the
    #: effective number to do it — a null column would silently fall back to a
    #: hardcoded 60 there and disagree with the server for every deployment that
    #: changed the interval.
    health_interval_effective_s: int
    #: Stored value: up | down | unknown.
    health_status: str
    #: Derived on read: adds stale (worker dead) and off (checks disabled), §8.
    status: str
    health_checked_at: datetime | None
    health_changed_at: datetime | None
    health_consec_failures: int
    health_consec_successes: int
    created_at: datetime
    updated_at: datetime

    # --- Location, manual vs derived (§4a) ---------------------------------
    #
    # `host_label` above is the PINNED value: operator-typed, and enrichment
    # never writes it. These three are the derived half, and they are separate
    # fields rather than one merged string on purpose — §4a's rule is
    # "disagreement is shown, not resolved", and a merged value would have had
    # to resolve it.
    #
    #: Derived location from the provider link, e.g. `pve-c · CT 105`. Null
    #: when nothing matched, which is a normal state — a service on hardware
    #: outside the cluster has no location and never will.
    location_derived: str | None
    #: Which provider derived it, so the UI can say where the value came from
    #: rather than presenting it as fact from nowhere.
    location_provider: str | None
    #: True only when BOTH exist and differ. Not "pinned is set": a pin that
    #: agrees with the derived value is not a disagreement and must not show a
    #: badge, or every correctly-pinned service nags forever.
    location_disagrees: bool
    #: True when the provider has not reported the linked guest for three
    #: consecutive successful polls (audit S8). The derived value is still
    #: served — stale-and-said beats silently current-looking — and after
    #: `ServiceLink.UNLINK_AFTER_MISSES` passes the link is dropped entirely.
    #: The UI treatment is Window 3's; `/export/json` carries the truth now.
    location_stale: bool

    @staticmethod
    def resolve_status(obj) -> str:
        return obj.effective_status()

    @staticmethod
    def resolve_health_interval_effective_s(obj) -> int:
        return obj.effective_interval_s()

    @staticmethod
    def _link(obj):
        """The first provider link, from prefetched rows where available.

        `obj.links.all()` rather than `.first()` so a `prefetch_related("links")`
        on the list endpoint is actually used — `.first()` issues a fresh query
        per service and would put one query per row behind a catalog the UI
        polls every 15 seconds.
        """
        links = list(obj.links.all())
        return links[0] if links else None

    @staticmethod
    def resolve_location_derived(obj) -> str | None:
        link = ServiceOut._link(obj)
        return (link.location or None) if link else None

    @staticmethod
    def resolve_location_provider(obj) -> str | None:
        link = ServiceOut._link(obj)
        return link.provider if link else None

    @staticmethod
    def resolve_location_disagrees(obj) -> bool:
        link = ServiceOut._link(obj)
        derived = (link.location if link else "") or ""
        pinned = (obj.host_label or "").strip()
        return bool(pinned and derived and pinned != derived)

    @staticmethod
    def resolve_location_stale(obj) -> bool:
        link = ServiceOut._link(obj)
        return bool(link and link.location_stale)


class HealthOut(Schema):
    slug: str
    status: str
    health_status: str
    health_enabled: bool
    health_checked_at: datetime | None
    health_changed_at: datetime | None
    health_consec_failures: int

    @staticmethod
    def resolve_status(obj) -> str:
        return obj.effective_status()


class WorkerFreshnessOut(Schema):
    """One worker's liveness, as `/api/v1/healthz` reports it (§5a).

    The first four fields are public: a watcher outside the container needs to
    know a worker stopped, and "the health worker is stalled" discloses nothing
    an anonymous caller cannot already see by watching every service go `stale`.

    **Everything below them is admin-only and null for anonymous callers.**
    `interval_s` and `stale_after_s` are the poll interval and 3x it, and
    `health.default_interval_s` is deliberately absent from the public settings
    whitelist (§6a) — publishing the threshold would hand back the value the
    whitelist withholds. `cycles` and `started_at` describe process internals in
    the same class.
    """

    name: str
    state: str
    stale: bool
    last_beat_at: datetime
    interval_s: int | None = None
    stale_after_s: int | None = None
    age_s: int | None = None
    started_at: datetime | None = None
    cycles: int | None = None


class HealthzOut(Schema):
    """`ok` is false when any registered worker is stale; the response is then
    served with **503**, so a watcher can alert on the status line alone without
    parsing the body."""

    ok: bool
    checked_at: datetime
    workers: list[WorkerFreshnessOut]
    #: Admin-only. Whether a Discord webhook actually resolves — presence only,
    #: never the URL, which is a credential.
    notifications_configured: bool | None = None


class AttentionServiceOut(Schema):
    """One service the status panel names as needing attention.

    Three fields, not a whole `ServiceOut`: the panel is a glance, and the
    drawer fills in the rest from the catalog the app has already loaded.
    """

    slug: str
    name: str
    status: str


class StatusSummaryOut(Schema):
    """§8's status summary panel. Derived exactly as the catalog derives it."""

    total: int
    up: int
    down: int
    stale: int
    unknown: int
    off: int
    #: Down, then stale, then unknown — capped. `off` is never listed: checks
    #: being disabled is a decision, not a fault.
    attention: list[AttentionServiceOut] = []
    #: How many there are in total, so the panel can say "and 4 more".
    attention_total: int = 0


class ClusterPanelOut(Schema):
    """§7d's summary row — cluster health, compute, storage.

    Raw numbers with explicit units; the browser formats them. `quorate` is
    `None` on a standalone host, which is not the same as `False`.
    """

    name: str
    quorate: bool | None
    ha: str
    nodes_online: int
    nodes_total: int
    cpu_pct: float | None
    cpu_count: int
    mem_used_bytes: int
    mem_total_bytes: int
    mem_pct: float | None
    storages_online: int
    storages_total: int
    storage_used_bytes: int
    storage_total_bytes: int
    storage_pct: float | None
    vm_count: int
    lxc_count: int


class NodePanelOut(Schema):
    """One row of §7d's nodes table.

    Every measurement is nullable: a node can be listed and still not answer the
    per-node read that carries load average and version, and a row with eight of
    nine columns beats a table that drops the node.

    **`ssl_fingerprint` is deliberately absent.** Proxmox's `/nodes` returns one
    per node and this response is public, so the field is omitted at the seam
    (`providers.base.Node`) rather than filtered here.
    """

    name: str
    online: bool
    #: **Deliberately public** (operator ruling 2026-08-07) so the drawer can link
    #: to the node's own web UI. This extends the 2026-08-06 cluster-data ruling
    #: from names and capacity to addresses; see AS_BUILT §7.
    ip: str = ""
    uptime_s: int | None = None
    cpu_pct: float | None = None
    cpu_count: int | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    load_avg: float | None = None
    storage_used_bytes: int | None = None
    storage_total_bytes: int | None = None
    version: str = ""


class ProxmoxPanelOut(Schema):
    """The cached provider state behind both Proxmox panels.

    `detail` is a **closed set of public strings** (`panels.py`), never the
    snapshot's own `detail`: that one is written for the admin-scoped settings
    screen and can name an environment variable or quote a `pveum` command
    carrying the token's `user@realm`.
    """

    available: bool
    detail: str
    polled_at: datetime | None
    stale: bool
    #: Null when that panel is switched off — the payload is omitted, not hidden
    #: client-side, so a hidden panel discloses nothing.
    cluster: ClusterPanelOut | None = None
    nodes: list[NodePanelOut] | None = None


# --- Adapter bar + curated Overview (TN4/TN3) ------------------------------


class AdapterOut(Schema):
    """One pill in the adapter bar — `Overview` plus one per configured provider.

    `health` is a closed set: `""` (Overview, which carries no dot), `ok`, `warn`
    or `err`, derived server-side from the cached snapshot so the pill can show a
    degraded adapter without the browser recomputing it.
    """

    key: str
    label: str
    health: str


class OverviewStatOut(Schema):
    """One curated headline stat on an Overview tile.

    `value` is a **display-ready** summary (`"3 / 4"`, `"Healthy"`, `"72%"`) — the
    Overview is a glance, not the raw numeric pipe the section panels expose.
    `tone` is an optional `ok`/`warn`/`err` colour hint.
    """

    label: str
    value: object
    tone: str = ""


class OverviewAdapterOut(Schema):
    """One configured adapter's curated summary, for the Overview branch."""

    key: str
    label: str
    health: str
    stats: list[OverviewStatOut] = []


class OverviewOut(Schema):
    """§8's curated cross-adapter summary — always present, from cache only."""

    adapters: list[OverviewAdapterOut] = []


class TrueNASSystemOut(Schema):
    """The System card: version, uptime, memory and load.

    Same disclosure class as the Proxmox node data already public here — no
    serials, no free text. `mem_used_bytes`/`mem_pct` are null until the reporting
    API is wired (best-effort, per the provider).
    """

    version: str
    hostname: str
    uptime_s: int | None
    mem_used_bytes: int | None
    mem_total_bytes: int | None
    mem_pct: float | None
    load_avg: float | None
    cores: int | None


class TrueNASPoolOut(Schema):
    """One pool row. `used_pct` is computed server-side (§8: a percentage
    computed twice in two places will disagree once)."""

    name: str
    status: str
    healthy: bool
    used_bytes: int | None
    total_bytes: int | None
    free_bytes: int | None
    used_pct: float | None
    scan_function: str
    scan_state: str
    scan_percentage: float | None


class TrueNASDiskOut(Schema):
    """One disk row.

    **No `serial` field, deliberately.** TrueNAS's `/disk` row carries a serial;
    the provider omits it at the `Disk` dataclass level (structural, like `Node`
    dropping `ssl_fingerprint`) and this schema does not reintroduce it. A
    disclosure test pins that serials never reach this endpoint — do not "fix"
    the absence.
    """

    name: str
    model: str
    disk_type: str
    size_bytes: int | None
    pool: str
    temperature_c: int | None
    smart_status: str


class TrueNASBackupOut(Schema):
    """One backup-task (replication / cloud-sync) last-run outcome."""

    kind: str
    name: str
    state: str
    at: datetime | None


class TrueNASAlertOut(Schema):
    """A native TrueNAS alert, **coarsened** for this public endpoint.

    **No `message` field, deliberately.** The provider's raw `formatted` alert
    text names pools/datasets and is attacker-influenceable (TN1's forward
    obligation), so the public row is `level` + `category` + time only.
    `category` is TrueNAS's `klass` — a provider-defined type identifier
    (`SMARTError`, `ZpoolCapacityNotice`) with no such content. The raw text stays
    cached for the `sanitize()`d Discord path; a disclosure test pins that it
    never appears on `/panels`.
    """

    level: str
    category: str
    at: datetime | None


class TrueNASPanelOut(Schema):
    """The cached TrueNAS state behind the TrueNAS adapter section.

    `detail` is a **closed set of public strings** (`panels.py`), never the
    snapshot's own `detail`, which can name `TRUENAS_HOST` or quote an httpx
    error and is admin-scoped on the settings screen. Each sub-block is null when
    its `ui.panels.truenas_*` toggle is off — omitted server-side, so a hidden
    panel discloses nothing. `backups` has no toggle and rides along when the
    section is available.
    """

    available: bool
    detail: str
    polled_at: datetime | None
    stale: bool
    hostname: str
    system: TrueNASSystemOut | None = None
    pools: list[TrueNASPoolOut] | None = None
    disks: list[TrueNASDiskOut] | None = None
    backups: list[TrueNASBackupOut] | None = None
    alerts: list[TrueNASAlertOut] | None = None


class PanelsOut(Schema):
    """§8's one panel endpoint. Assembled server-side from cached state only."""

    landing_page: str
    #: Visible panel keys in render order. The frontend renders from this list,
    #: so switching a panel off in settings removes it from the page.
    panels: list[str]
    #: The adapter bar (TN4): Overview + one pill per configured provider. Always
    #: present — an unconfigured lab still gets a bar with just Overview.
    adapters: list[AdapterOut] = []
    #: The curated cross-adapter summary the Overview pill shows. Always present.
    overview: OverviewOut | None = None
    status_summary: StatusSummaryOut | None = None
    #: Null when Proxmox is not configured (§7d) or both its panels are off.
    proxmox: ProxmoxPanelOut | None = None
    #: Null when TrueNAS is not configured (§7d) or all four panels are off.
    truenas: TrueNASPanelOut | None = None


class SavedViewIn(StrictSchema):
    name: str
    query: str
    #: Bounded (this window's api-security gate): views skip `full_clean`, so
    #: unlike the Service int columns nothing downstream range-checks this —
    #: an out-of-range int was a DataError 500 instead of the contract's 422.
    position: int | None = Field(default=None, ge=0, le=1_000_000)


class SavedViewRename(StrictSchema):
    """Rename only. A view's *query* is deliberately not editable here — see the
    endpoint."""

    name: str


class SavedViewOut(Schema):
    id: UUID
    name: str
    query: str
    position: int


class ProbeIn(StrictSchema):
    url: str


class ProbeOut(Schema):
    ok: bool
    url: str
    final_url: str
    status: int | None
    title: str | None
    icon_url: str | None
    redirects: list[str]
    error: str | None
    #: Slug the catalog would derive from this URL — lets the UI preview the
    #: upsert key without reimplementing the rule.
    suggested_slug: str
    suggested_name: str | None
    #: Location this URL **would** derive from the provider cache, or null
    #: (v1.4). Shown beside the probe result, never written into the Location
    #: input: that input is the *pinned* value, and pre-filling it would pin
    #: every service at creation so the derived value could never track (§4a).
    #: Null covers every honest case — nothing cached, no guest at that address,
    #: or an address held by more than one guest — each of which is exactly what
    #: the service will show after saving.
    derived_location: str | None
    derived_location_provider: str | None


class TaxonomyRename(StrictSchema):
    name: str


class TaxonomyOut(Schema):
    #: The value after the change; empty when it was removed.
    name: str
    #: How many services were touched — lets the UI confirm the blast radius.
    affected: int


class IconIn(StrictSchema):
    url: str


class IconOut(Schema):
    #: Local media path, never the remote URL — see api.fetch_icon.
    icon_url: str


class SessionIn(StrictSchema):
    token: str


class SessionOut(Schema):
    scope: str


class SlugPreviewOut(Schema):
    slug: str


class MessageOut(Schema):
    detail: str


# --- Settings (v1 §4, §6a) -------------------------------------------------
#
# The settings PATCH body is a bare `{key: value}` map, so `extra="forbid"`
# cannot be what rejects an unknown key — the key set is data, not a class
# definition. The registry in `settings_store.py` enforces the same contract
# instead: an unrecognised key comes back as a 422 naming it, never silently
# dropped. Worth stating, because "no StrictSchema here" otherwise reads as an
# oversight in a file whose whole point is strictness.


class SettingOut(Schema):
    """One registry key, with everything the screen needs to render its row."""

    key: str
    kind: str
    value: object
    default: object
    label: str
    helper: str
    section: str
    unit: str | None = None
    minimum: int | None = None
    step: int | None = None
    choices: list[str] = []
    #: The chunk that consumes this value. Present so the screen can say so
    #: rather than implying a control is live when its consumer has not shipped.
    consumer: str = ""


class EnvControlOut(Schema):
    """A deploy-time control the screen shows but does not own (§6a)."""

    label: str
    env_var: str
    #: Either a displayable value or "Configured" / "Not configured". A secret's
    #: value is never read on this path at all.
    status: str
    shows_value: bool
    note: str


class ProviderStatusOut(Schema):
    """One location provider's health, for the settings screen (§7a, §7d).

    **Admin-scoped**, and it ships in the admin settings response rather than
    anywhere public: guest counts, node names and privilege names are
    infrastructure disclosure of a different class from the catalog, and §6a
    already draws that line for the `.env` panel.

    **Served entirely from the cached snapshot.** Rendering this must not make
    the web container call Proxmox — §7c's "clients read the cached value" is
    about the request path generally, and a settings screen that polls the
    cluster is exactly the shape §8 warns gets rebuilt.
    """

    name: str
    configured: bool
    #: False with `configured` true means the credential authenticates but holds
    #: no privileges — Proxmox answers `200 []` for collections it filters, so
    #: without this an under-privileged token is indistinguishable from an empty
    #: cluster. See `providers/base.py`.
    usable: bool
    #: Human-readable reason when not usable, including the exact `pveum`
    #: commands for the blind-token case. Never contains a credential.
    detail: str
    #: Why the CLUSTER read failed while the guest list still worked (audit
    #: S5) — the case where `usable` stays true, the dashboard panel says "see
    #: Settings", and this field is what there is to see. Empty when the
    #: cluster read works. Admin-scoped like everything else in this row.
    infrastructure_detail: str = ""
    #: Privilege names the credential held at the last successful poll, from
    #: the cached snapshot (audit informational — computed and asserted since
    #: V1.4, serialised nowhere until now). Empty until a poll has succeeded;
    #: on a failing credential the diagnosis lives in `detail`, which already
    #: names the exact grants to run. Admin-scoped like the rest of this row —
    #: `/panels` never carries it.
    privileges: list[str] = []
    guest_count: int
    #: Addresses held by more than one guest and therefore unusable for
    #: matching — almost always Docker bridge gateways. Surfaced because "why
    #: did nothing match" is otherwise unanswerable from the UI.
    ambiguous_address_count: int
    last_polled_at: datetime | None


class SettingsOut(Schema):
    auth_mode: str
    settings: list[SettingOut]
    environment: list[EnvControlOut]
    sections: list[str]
    providers: list[ProviderStatusOut]


class PublicSettingsOut(Schema):
    """What an anonymous viewer may read (§6a).

    `auth_mode` rides along because §12 requires the current mode to be visible
    outside the settings screen, and the catalog header is anonymous. Under
    `full` it discloses nothing useful; under an open mode anyone can already
    write, so it discloses nothing they could not discover in one request.
    """

    auth_mode: str
    values: dict[str, object]
