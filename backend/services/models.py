"""Data model — spec §4.

Field names and types follow §4 verbatim. `provenance` and `ignored` are unused
in v0 but present from migration 1 so v0.2 discovery needs no schema change.
"""

import re
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils import timezone

# Effective (read-time) status values. `stale` and `off` are never stored —
# see Service.effective_status.
STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"
STATUS_STALE = "stale"
STATUS_OFF = "off"

#: Inline icons: base64 `data:image/…` only — the base64 alphabet cannot carry
#: a quote or an angle bracket, so the value is attribute-safe by construction.
#: Identical to the rule the recovery export and the probe enforce at their own
#: seams (audit B3); stated at the write path too so the database cannot hold a
#: value a sink would have to refuse.
_DATA_IMAGE_ICON_RE = re.compile(
    r"^data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]*$", re.IGNORECASE
)


def _default_timeout_ms():
    return settings.HEALTH_DEFAULT_TIMEOUT_MS


# `_default_interval_s` and `_default_fail_threshold` are referenced by
# `0001_initial` and by nothing else. V1.3 made both columns nullable — null
# means "follow the global setting" — so a new row must arrive *unstamped*, and
# a model-level default is exactly what stamping is. They stay defined because
# Django's historical migrations must be able to import what they name; deleting
# them would break `migrate` on a fresh database. See migration 0003.
def _default_interval_s():
    return settings.HEALTH_DEFAULT_INTERVAL_S


def _default_fail_threshold():
    return settings.HEALTH_DEFAULT_FAIL_THRESHOLD


class Service(models.Model):
    class Provenance(models.TextChoices):
        MANUAL = "manual"
        API = "api"
        DISCOVERED = "discovered"
        MANIFEST = "manifest"

    class LaunchTarget(models.TextChoices):
        NEW_TAB = "new_tab"
        SAME_TAB = "same_tab"

    class HealthStatus(models.TextChoices):
        UP = STATUS_UP
        DOWN = STATUS_DOWN
        UNKNOWN = STATUS_UNKNOWN

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Immutable after creation (spec §4): if the slug tracked the URL, changing a
    # service's address would produce a second record on the next agent write.
    slug = models.TextField(unique=True)
    name = models.TextField()

    launch_url = models.TextField()
    launch_url_external = models.TextField(null=True, blank=True)
    # A COMPLETE probe URL, not a base — no path field, no URL joining (§4).
    # Null means "use launch_url".
    health_url = models.TextField(null=True, blank=True)

    category = models.TextField(null=True, blank=True)
    tags = ArrayField(models.TextField(), default=list, blank=True)
    icon_url = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    host_label = models.TextField(null=True, blank=True)
    provenance = models.TextField(
        choices=Provenance.choices, default=Provenance.MANUAL
    )
    created_by = models.TextField(null=True, blank=True)
    ignored = models.BooleanField(default=False)

    repo_url = models.TextField(null=True, blank=True)
    docs_url = models.TextField(null=True, blank=True)

    launch_target = models.TextField(
        choices=LaunchTarget.choices, default=LaunchTarget.NEW_TAB
    )

    health_enabled = models.BooleanField(default=True)
    # Loud on the dashboard without waking you (§4). Deliberately separate from
    # `health_enabled`: a service can be worth a red pill and not worth a 03:00
    # Discord message.
    notify_enabled = models.BooleanField(default=True)
    # Null means any HTTP response counts as up (§8).
    health_expect_status = models.IntegerField(null=True, blank=True)
    # Null on these two means "follow the global setting" — `health.default_interval_s`
    # and `health.fail_threshold` in the settings table. A typed number pins this
    # service to it. Without the null the settings screen's "applies to every
    # service without its own interval" describes a state no service can be in,
    # because every row is stamped with a value at insert.
    health_interval_s = models.IntegerField(null=True, blank=True)
    health_timeout_ms = models.IntegerField(default=_default_timeout_ms)
    health_fail_threshold = models.IntegerField(null=True, blank=True)

    health_status = models.TextField(
        choices=HealthStatus.choices, default=HealthStatus.UNKNOWN
    )
    health_checked_at = models.DateTimeField(null=True, blank=True)
    health_changed_at = models.DateTimeField(null=True, blank=True)
    health_consec_failures = models.IntegerField(default=0)
    # Symmetric with the failure counter: up-transitions are damped too (§5), or
    # one flapping service floods the channel until somebody mutes it.
    health_consec_successes = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "services"
        ordering = ["name"]

    def __str__(self):
        return self.slug

    #: Minimum seconds between checks for a service that pins its own interval.
    #: The same floor §6a puts on the global setting, enforced here because V1.3
    #: made the per-service column meaningful and the settings floor never
    #: covered this write path. See `clean()`.
    MIN_INTERVAL_S = 15

    #: Bounds on the per-service check timeout (audit S1). The floor exists
    #: because httpx accepts a negative Timeout and `asyncio.wait_for` raises
    #: immediately on one — `-5000` fails every check instantly and turns the
    #: whole catalog `down` in one write. The ceiling exists because
    #: `POST /health/{slug}/check` runs the check on the request thread, so a
    #: 2147483647 ms timeout is a gunicorn worker held for ~24.8 days; three
    #: such requests exhaust the container.
    MIN_TIMEOUT_MS = 250
    MAX_TIMEOUT_MS = 60_000

    #: Length ceilings on the writable text columns (audit S12). Before these,
    #: every string was an unconstrained TextField and tags an unbounded
    #: text[], so an agent token — anonymous under `open-writes` — could park
    #: megabytes on a record that the public catalog then re-serves on every
    #: 15-second poll and the browser filters client-side: the cost outlives
    #: the writer. Generous for people, refusing only machine-scale abuse.
    MAX_NAME_LEN = 200
    MAX_SLUG_LEN = 100
    MAX_URL_LEN = 2000
    MAX_DESCRIPTION_LEN = 4000
    MAX_TAGS = 32
    MAX_TAG_LEN = 100

    @classmethod
    def max_icon_url_len(cls) -> int:
        """`icon_url` legitimately holds an inline base64 data URI, so its
        ceiling derives from the inline-icon byte cap rather than being a
        second number that can drift from it: base64 is 4/3 of the bytes, plus
        prefix headroom."""
        return settings.ICON_MAX_INLINE_BYTES * 4 // 3 + 64

    @staticmethod
    def _is_http_url(value: str) -> bool:
        """A full http(s) URL with a host.

        Stated positively on purpose: `urlsplit`'s treatment of `host:port`
        (is `host` a scheme?) has flip-flopped across Python versions, so
        nothing here classifies *why* a value fails — it either parses as
        http(s)-with-host or it is refused.
        """
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    @classmethod
    def _is_acceptable_icon(cls, value: str) -> bool:
        """The three shapes `icon_url` legitimately holds: a remote http(s)
        URL, a base64 `data:image/…` URI (inline SVGs), or the app's own
        cached-icon path (`/media/icons/<digest>.<ext>`, written by
        `probe._store_icon` — MEDIA_URL is read live, and Django serves the
        relative setting with a leading slash)."""
        if cls._is_http_url(value):
            return True
        if _DATA_IMAGE_ICON_RE.match(value):
            return True
        return (
            re.fullmatch(
                re.escape(settings.MEDIA_URL) + r"icons/[A-Za-z0-9._-]+", value
            )
            is not None
        )

    def clean(self):
        """Floors on the per-service health values (§6a, added by V1.3's gate).

        V1.2 put a 15s floor on `health.default_interval_s` because "the UI
        stepper is a convenience, the API is the boundary" — but that floor lives
        on the *settings* write path, and this is a different write path.
        Pinning `health_interval_s: 1` makes the worker probe that service every
        second, which is a 60x amplification of §4c's deliberately-sanctioned
        "an agent write makes the server issue an outbound request", sustained
        rather than one-shot, and reachable **anonymously** under `open-writes`.

        Raised as a Django `ValidationError` so `on_model_validation_error`
        turns it into the 422 naming the field that §4c requires, on both the
        create and the patch path, without either endpoint remembering to ask.
        """
        errors = {}
        if (
            self.health_interval_s is not None
            and self.health_interval_s < self.MIN_INTERVAL_S
        ):
            errors["health_interval_s"] = (
                f"Minimum {self.MIN_INTERVAL_S} seconds. Leave it unset to follow "
                "the interval in Settings."
            )
        if self.health_fail_threshold is not None and self.health_fail_threshold < 1:
            errors["health_fail_threshold"] = (
                "Must be at least 1. Leave it unset to follow the threshold in "
                "Settings."
            )
        if self.health_timeout_ms is not None and not (
            self.MIN_TIMEOUT_MS <= self.health_timeout_ms <= self.MAX_TIMEOUT_MS
        ):
            errors["health_timeout_ms"] = (
                f"Between {self.MIN_TIMEOUT_MS} and {self.MAX_TIMEOUT_MS} "
                "milliseconds."
            )
        # Audit S12 — enforced here so create and patch are both covered
        # without either endpoint remembering to ask, like every bound above.
        for field_name, limit in (
            ("name", self.MAX_NAME_LEN),
            ("slug", self.MAX_SLUG_LEN),
            ("category", self.MAX_NAME_LEN),
            ("host_label", self.MAX_NAME_LEN),
            ("created_by", self.MAX_NAME_LEN),
            ("description", self.MAX_DESCRIPTION_LEN),
            ("launch_url", self.MAX_URL_LEN),
            ("launch_url_external", self.MAX_URL_LEN),
            ("health_url", self.MAX_URL_LEN),
            ("repo_url", self.MAX_URL_LEN),
            ("docs_url", self.MAX_URL_LEN),
        ):
            value = getattr(self, field_name)
            if value and len(value) > limit:
                errors[field_name] = f"At most {limit} characters."
        if self.icon_url and len(self.icon_url) > self.max_icon_url_len():
            errors["icon_url"] = f"At most {self.max_icon_url_len()} characters."
        tags = self.tags or []
        if len(tags) > self.MAX_TAGS:
            errors["tags"] = f"At most {self.MAX_TAGS} tags."
        elif any(len(tag or "") > self.MAX_TAG_LEN for tag in tags):
            errors["tags"] = f"Tags are limited to {self.MAX_TAG_LEN} characters each."
        # Scheme allowlists on the URL columns (operator ruling 2026-08-10, the
        # pre-release breaking change). A URL column is something a browser
        # eventually navigates, and `<a href="javascript:…">` executes with
        # this app's CSRF token readable by design — so the database must not
        # be able to hold one. Every sink already refuses these (`safeUrl`,
        # the recovery export, the Discord embed); the write path joining them
        # means a future renderer cannot be wrong. Refused, never silently
        # normalized: a 422 naming the fix teaches, mutation hides typos.
        for field_name in (
            "launch_url",
            "launch_url_external",
            "repo_url",
            "docs_url",
        ):
            value = getattr(self, field_name)
            if value and not self._is_http_url(value):
                errors.setdefault(
                    field_name,
                    "Must be a full http:// or https:// URL, including the scheme.",
                )
        if self.health_url and not (
            self._is_http_url(self.health_url)
            or (
                urlsplit(self.health_url).scheme == "tcp"
                and urlsplit(self.health_url).netloc
            )
        ):
            errors.setdefault(
                "health_url",
                "Must be an http://, https:// or tcp:// URL, including the scheme.",
            )
        if self.icon_url and not self._is_acceptable_icon(self.icon_url):
            errors.setdefault(
                "icon_url",
                "Must be an http(s) URL, a base64 data:image/… URI, or an icon "
                "cached by this app.",
            )
        if errors:
            raise DjangoValidationError(errors)

    @property
    def clamped_timeout_ms(self) -> int:
        """The timeout the checkers actually honour.

        `clean()` bounds the API write path; a row written around it (manual
        psql, a migration bug) must not hand the worker an unbounded
        `httpx.Timeout` — the same two-layer shape as the auth-mode coercion,
        and like it, failing closed to a sane value rather than trusting the
        table (audit S1).
        """
        return min(max(self.health_timeout_ms or 0, self.MIN_TIMEOUT_MS),
                   self.MAX_TIMEOUT_MS)

    @property
    def probe_url(self) -> str:
        """`health_url` defaults to `launch_url` when unset (§4)."""
        return self.health_url or self.launch_url

    @property
    def check_type(self) -> str:
        """Key into the health check-type registry (§8).

        Derived from the probe URL's scheme rather than stored in a column: §4's
        schema lists no check-type field, and the scheme already carries the
        information. `tcp://host:port` selects the tcp checker; anything else
        uses http.
        """
        scheme = urlsplit(self.probe_url).scheme.lower()
        return "tcp" if scheme == "tcp" else "http"

    def effective_interval_s(self, policy=None) -> int:
        """This service's poll interval: its own if pinned, else the global.

        `health_interval_s` is nullable as of V1.3 and null is what a new service
        gets, so nothing may read the column directly and expect a number.

        Resolving the global costs a query, and the catalog serialises every
        service on every poll — so a caller with a list of services attaches one
        `hlv_policy` to each (see `api._with_policy`) and this method uses it.
        With nothing attached it reads live, which is what every single-object
        path does.
        """
        policy = policy or getattr(self, "hlv_policy", None)
        if policy is None:
            from .health.policy import current_policy

            policy = current_policy()
        return policy.interval_for(self)

    def effective_status(self, now=None, policy=None) -> str:
        """Status as the API reports it (§8).

        `stale` is derived on read and never stored, because the condition that
        produces it is *the health worker being dead* — and a dead worker cannot
        write `stale` to the database. Deriving it means a stopped worker
        surfaces immediately and for free.
        """
        if not self.health_enabled:
            return STATUS_OFF
        if self.health_checked_at is None:
            return STATUS_UNKNOWN
        now = now or timezone.now()
        stale_after = timedelta(seconds=3 * self.effective_interval_s(policy))
        if now - self.health_checked_at > stale_after:
            return STATUS_STALE
        return self.health_status


class ServiceLink(models.Model):
    """Provider-neutral seam (§4). No provider is named in the schema, so adding
    one requires no migration. Created in v0, **populated in V1.4**.

    **This is where derived location lives, and it is never copied onto
    `services` as a string** (§4, §7b). Guests migrate between nodes; a copied
    string goes stale silently and nothing notices. Keeping it here means the
    next poll sees the guest on its new node and the link updates — no re-probe,
    no manual step. That is the entire reason this table exists.

    `ref` holds the **foreign ID** — Proxmox's VMID — and is the identity a link
    is refreshed by. The address is deliberately not identity: it moves with
    DHCP and NIC changes, so it matches a service once and is never trusted for
    identity again (§7b).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="links"
    )
    provider = models.TextField()
    kind = models.TextField()
    ref = models.TextField()
    payload = models.JSONField(default=dict, blank=True)
    #: When the provider last confirmed this guest exists — distinct from
    #: `updated_at`, which only moves when a field changes. A guest that vanishes
    #: from the cluster stops being seen without anything about the row changing,
    #: so `updated_at` cannot express "still there". Also load-bearing for the
    #: stopped-guest case: 4 of 36 containers in the reference lab were stopped
    #: and reported no address, and a stopped guest is not a deleted one.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    #: Consecutive successful refresh passes in which the provider did NOT
    #: report this guest (audit S8) — zeroed every time it is seen. Counted in
    #: passes rather than wall-clock so it moves only when the provider
    #: actually answered: a stopped worker ages every link equally and says
    #: nothing about any guest, and the panel already reports that state.
    missed_passes = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    #: The app's standing 3× rule, in passes: three consecutive polls without
    #: the guest and the derived location is announced as stale rather than
    #: presented as current fact (audit S8).
    STALE_AFTER_MISSES = 3
    #: A guest missing this long (~2 h at the default 360 s interval) is gone,
    #: not rebooting: the link is dropped and the service returns to the
    #: address-match pool. Deletion is honest here where it was not for
    #: un-configuring the provider — the provider itself kept answering that
    #: the guest no longer exists.
    UNLINK_AFTER_MISSES = 20

    class Meta:
        db_table = "service_links"
        constraints = [
            # One link per provider per service. Without this, a matcher bug
            # writes a second link instead of updating the first and the UI
            # shows two locations for one service with no way to tell which is
            # current.
            models.UniqueConstraint(
                fields=["service", "provider"], name="service_links_service_provider_uniq"
            )
        ]
        indexes = [
            # Refresh-by-foreign-id is the hot path (§7b): every poll looks up
            # links by (provider, ref) to update the node.
            models.Index(fields=["provider", "ref"], name="service_links_provider_ref"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.ref}"

    @property
    def node(self) -> str:
        return (self.payload or {}).get("node") or ""

    @property
    def location(self) -> str:
        """The derived location string, as stored by the enrichment pass."""
        return (self.payload or {}).get("location") or ""

    @property
    def location_stale(self) -> bool:
        """Whether the derived location should be announced as stale (audit S8)."""
        return self.missed_passes >= self.STALE_AFTER_MISSES


class ProviderSnapshot(models.Model):
    """The cached result of one provider poll — v1 §7c.

    §7c: *"Polls the cluster on its own interval, caches the result, and
    refreshes `service_links` by VMID. Clients read the cached value; they do
    not each poll Proxmox."*

    **In Postgres, not in memory or on the worker's disk**, for the same reason
    `worker_heartbeats` is: web and worker are separate containers, so a cache
    on the worker's filesystem is invisible to the web request path that has to
    serve it. This is v0's doc-drift 1g repeating, and it is pre-empted here
    rather than rediscovered.

    **This is also what makes §7c's "Proxmox unreachable never blocks a catalog
    write or a probe" structural rather than defensive.** Request paths read
    this table; they never call a provider. There is no timeout to tune and no
    try/except to forget, because there is no outbound call on the request path
    at all.

    `ok` and `detail` carry a *failed* poll rather than discarding it: an
    operator asking "why is there no location" needs the reason, and a snapshot
    that simply stops updating cannot give one. The previous `payload` is left
    intact on failure — stale data plus a stated staleness beats no data.
    """

    #: The provider's `name`, one row each.
    provider = models.TextField(primary_key=True)
    payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField()
    ok = models.BooleanField(default=True)
    detail = models.TextField(blank=True, default="")
    #: Alert damping state (BACKLOG.6), same shape as the service thresholds:
    #: one failed poll is a transient, a streak is an incident. `alert_active`
    #: is the edge-trigger memory — announce the fall once, the recovery once.
    consec_failures = models.IntegerField(default=0)
    alert_active = models.BooleanField(default=False)

    class Meta:
        db_table = "provider_snapshots"
        ordering = ["provider"]

    def __str__(self):
        return self.provider

    @property
    def guests(self) -> list:
        return (self.payload or {}).get("guests") or []


class StaticExportState(models.Model):
    """Cross-container coordination for the recovery export — audit S7.

    One row, id=1. `pending` is the coalescing flag: every export request sets
    it before trying the advisory lock, the lock holder clears it before
    reading the catalog, and re-checks it after publishing — so a mutation
    that lands while an export is mid-flight produces exactly one follow-up
    export instead of a pile-up of racing threads, and a request that loses
    the lock can simply leave, knowing the holder will re-run for it.

    **In Postgres, not in module state**, for the standing reason: three web
    workers and the health worker's retry live in different processes and two
    different containers, and every purely in-process guard is invisible to
    the writer that matters. Same lesson as `worker_heartbeats` and
    `provider_snapshots`.
    """

    id = models.SmallIntegerField(primary_key=True, default=1)
    pending = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "static_export_state"

    def __str__(self):
        return f"pending={self.pending}"


class SavedView(models.Model):
    """A saved view is a stored query string. Nothing more (§4).

    Live and fully used in v0 — §4b requires views to live in a table rather than
    localStorage.
    """

    #: Same S12 reasoning as the Service ceilings: `/views` is agent-writable
    #: (anonymous under `open-writes`) and the list is served to every
    #: anonymous page load, so an unbounded name or query is parked cost.
    MAX_NAME_LEN = 200
    MAX_QUERY_LEN = 2000

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(unique=True)
    query = models.TextField()
    position = models.IntegerField(default=0)

    class Meta:
        db_table = "saved_views"
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class Setting(models.Model):
    """Runtime preferences — v1 §4. The first app-level state that is not
    service data.

    **Namespaced keys, not feature-shaped columns**: `notifications.discord.enabled`,
    not a `discord_enabled` column. A column per preference means a migration
    per preference, and the set of preferences is the part most likely to grow.

    The table stores only what the registry in `settings_store.py` declares, and
    nothing in that registry can hold free text — so a credential has nowhere to
    live here. Values that *are* credentials are stored as the NAME of an
    environment variable and resolved at use time (§4).

    Rows are sparse: a key with no row uses its registry default, so a fresh
    install needs no seeding step and a new key ships with its default already
    in force.
    """

    key = models.TextField(primary_key=True)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "settings"
        ordering = ["key"]

    def __str__(self):
        return self.key


class WorkerHeartbeat(models.Model):
    """One row per background worker — the deadman's registry (§5a).

    **This table is V1.3's product and V1.4's input** (standing rule 2). The
    Proxmox worker registers against it unchanged, so the shape is deliberate:
    a second shape later is a migration.

    **In Postgres, not on a worker's disk.** Web and worker are separate
    containers, so a heartbeat written to the worker's filesystem is invisible to
    an evaluator on the web request path — the identical bug that stopped v0's
    export retry from ever firing (`BUILD_LOG.md` doc-drift 1g). `/api/v1/healthz`
    has to serve these values anyway.

    **The worker declares its own cadence.** `interval_s` is written on every
    beat from whatever the worker's interval setting says *at that moment*, and
    the evaluator multiplies by 3 at evaluation time. Two consequences worth
    stating: the threshold tracks a settings change without a restart (§5a), and
    the evaluator never needs to know which settings key belongs to which worker
    — which is what lets V1.4 register a worker driven by `proxmox.poll_interval_s`
    without touching the deadman.

    `state` and `alerted_at` make the alerting edge-triggered (§5a): a stalled
    worker is announced once, not on every evaluation cycle, or an overnight
    failure posts several hundred times before anyone wakes up.
    """

    class State(models.TextChoices):
        OK = "ok"
        STALLED = "stalled"

    #: Stable worker key: "health", and "proxmox" from V1.4.
    name = models.TextField(primary_key=True)
    #: The worker's poll interval as of the last beat. The deadman fires at 3x.
    interval_s = models.IntegerField()
    started_at = models.DateTimeField()
    last_beat_at = models.DateTimeField()
    #: Completed cycles since this process started — a counter that moves is how
    #: you tell a working worker from one wedged mid-cycle.
    cycles = models.BigIntegerField(default=0)
    state = models.TextField(choices=State.choices, default=State.OK)
    stalled_since = models.DateTimeField(null=True, blank=True)
    #: When the current state was announced. Null means "not yet announced".
    alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "worker_heartbeats"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def stale_after_s(self) -> int:
        """3 poll intervals (§5a) — tolerates a slow cycle plus a retry.

        Derived here rather than stored, and derived from the interval the worker
        last declared rather than from a constant: hardcoding 180 seconds goes
        quietly wrong the day somebody changes the interval.
        """
        return 3 * max(self.interval_s, 1)


class Discovered(models.Model):
    """Table created in v0, populated in v0.2 (§4). The claim inbox from §13."""

    class State(models.TextChoices):
        NEW = "new"
        CLAIMED = "claimed"
        IGNORED = "ignored"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.TextField()
    external_ref = models.TextField()
    label = models.TextField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    state = models.TextField(choices=State.choices, default=State.NEW)

    class Meta:
        db_table = "discovered"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_ref"], name="discovered_source_ref_uniq"
            )
        ]

    def __str__(self):
        return f"{self.source}:{self.external_ref}"
