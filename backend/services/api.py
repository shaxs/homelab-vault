"""HTTP API — spec §4c.

The API is the product's primary surface; the web UI is a client of it and gets
no privileged path to the database.

django-ninja 1.6 has no API-level CSRF switch — CSRF is a property of the auth
class, which is exactly the shape spec §4c asks for. Enforcement therefore lives
in the session-authentication branch of services/auth.py, and bearer requests
never reach it.
"""

import logging
import re
from urllib.parse import urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from ninja import Body, File, NinjaAPI, UploadedFile
from ninja.errors import HttpError

from . import deadman, enrichment, notifications, panels, settings_store
from .auth import (
    SCOPE_ADMIN,
    ReauthRequired,
    actor_name,
    admin_auth,
    admin_only_auth,
    agent_auth,
    check_login_throttle,
    clear_login_throttle,
    current_auth_mode,
    record_failed_login,
    request_is_admin,
    require_recent_admin_auth,
    scope_for_token,
    start_admin_session,
)
from .export_static import schedule_static_export
from .health.policy import current_policy
from .health.registry import run_check_bounded
from .health.transitions import apply_result, should_notify
from .models import ProviderSnapshot, SavedView, Service
from .probe import (
    ProbeRejected,
    fetch_and_store_icon,
    probe_url,
    store_icon_bytes,
    validate_url,
)
from .schemas import (
    HealthOut,
    HealthzOut,
    IconIn,
    IconOut,
    MessageOut,
    PanelsOut,
    ProbeIn,
    ProbeOut,
    PublicSettingsOut,
    SavedViewIn,
    SavedViewOut,
    SavedViewRename,
    SettingsOut,
    ServiceIn,
    ServicePatch,
    ServiceOut,
    SessionIn,
    SessionOut,
    SlugPreviewOut,
    TaxonomyOut,
    TaxonomyRename,
)
from .providers import PROVIDERS
from .slugs import derive_slug, is_ip_literal

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="Homelab Vault",
    version="0.1.0",
    description=(
        "Find a service, see if it's alive, open it. Reads are unauthenticated "
        "by design; writes require an agent or admin bearer token, or an admin "
        "session cookie."
    ),
)


@api.exception_handler(DjangoValidationError)
def on_model_validation_error(request, exc: DjangoValidationError):
    """Model-level validation must surface as a 422 naming the field, not a 500.

    Pydantic covers types and unknown keys, but a value that is the right type
    and the wrong *choice* — `launch_target: "popup_window"` — only fails at
    `Model.full_clean()`. Django raises its own ValidationError there, which
    Ninja does not recognise and re-raises as a 500. Spec §4c requires unknown
    or invalid input to come back as a 422 that names the offending field and is
    "readable enough for a model to self-correct on retry".
    """
    detail = [
        {"loc": ["body", "payload", field], "msg": message}
        for field, messages in exc.message_dict.items()
        for message in messages
    ]
    return api.create_response(request, {"detail": detail}, status=422)


@api.exception_handler(ReauthRequired)
def on_reauth_required(request, exc: ReauthRequired):
    """403 carrying a code the SPA can branch on (§6b).

    The write gate already opens on any 401/403, so without `code` the UI cannot
    tell "you are not signed in" from "confirm this deletion" — two situations
    that need different words in front of the same token field.
    """
    return api.create_response(
        request, {"detail": str(exc), "code": "reauth_required"}, status=403
    )


# Rough keyword -> category guesses for the fast path (§7). Free text with
# autocomplete is the real mechanism (§6b); this only seeds the field.
_CATEGORY_HINTS = {
    "media": ("jellyfin", "plex", "emby", "sonarr", "radarr", "navidrome", "audiobook"),
    "storage": ("nas", "truenas", "synology", "minio", "nextcloud", "syncthing"),
    "network": ("pihole", "unifi", "udm", "opnsense", "pfsense", "adguard", "traefik"),
    "monitoring": ("grafana", "prometheus", "uptime", "netdata", "zabbix"),
    "infra": ("proxmox", "portainer", "komodo", "docker", "rancher"),
    "ai": ("ollama", "openwebui", "llama", "comfyui", "automatic1111"),
    "tools": ("karakeep", "vaultwarden", "gitea", "paperless", "immich"),
}


# --- Public reads (no authentication, §4c) --------------------------------


def _with_policy(services: list[Service]) -> list[Service]:
    """Resolve the global health settings once for a whole response.

    Every service without a pinned interval follows the settings table, and both
    `status` and `health_interval_effective_s` need that number. Reading it per
    row would put one settings query per service behind a response the catalog
    polls every 15s. Read live, once, per response — which keeps §5a's "not
    snapshotted" guarantee: the value is never held across requests.
    """
    policy = current_policy()
    for service in services:
        service.hlv_policy = policy
    return services


def _catalog_queryset():
    """Every service, with provider links prefetched (§4a).

    The prefetch is load-bearing, not tidiness: `ServiceOut` resolves three
    location fields off `obj.links`, so without it a 46-service catalog issues
    46 extra queries on an endpoint the UI polls every 15 seconds.
    """
    return Service.objects.all().prefetch_related("links")


@api.get("/services", response=list[ServiceOut], auth=None, tags=["services"])
def list_services(request):
    """Full catalog. The whole thing ships on load (~60KB) and the §6 query
    syntax is applied in the browser (§3b), so there is no server-side filter."""
    return _with_policy(list(_catalog_queryset()))


@api.get("/services/{slug}", response=ServiceOut, auth=None, tags=["services"])
def get_service(request, slug: str):
    return get_object_or_404(_catalog_queryset(), slug=slug)


@api.get("/health/{slug}", response=HealthOut, auth=None, tags=["health"])
def get_health(request, slug: str):
    return get_object_or_404(Service, slug=slug)


@api.post("/health/{slug}/check", response=HealthOut, auth=agent_auth,
          tags=["health"])
def recheck_health(request, slug: str):
    """Run this service's health check now, instead of waiting for its interval.

    Authenticated because it makes the server issue an outbound request on
    demand; unauthenticated reads must not be able to drive network traffic.

    Uses the same registry and the same transition rules as the worker, so a
    manual check cannot produce a status the worker would disagree with — in
    particular a single failure still only counts once against
    `health_fail_threshold` rather than forcing an immediate `down`.
    """
    service = get_object_or_404(Service, slug=slug)
    if not service.health_enabled:
        raise HttpError(409, "Health checks are disabled for this service.")

    policy = current_policy()
    # Bounded (audit S1): this runs on the request thread, so an unbounded
    # check is a gunicorn worker held hostage — three of them is the whole
    # container. The ceiling is the model's own maximum plus slack.
    ok = async_to_sync(run_check_bounded)(service)
    applied = apply_result(service, ok, policy=policy)
    service.save(update_fields=applied.changed)
    # A manual recheck uses the same transition rules as the worker, so it can
    # produce the same transitions — and a status change nobody is told about is
    # a status change that never reaches Discord, because the worker's next pass
    # sees no *further* transition. Dispatched, so the outbound POST is not on
    # this request's critical path.
    if policy.discord_enabled and should_notify(service, applied):
        notifications.dispatch(
            notifications.service_transition_message(service, applied)
        )
    service.hlv_policy = policy
    return service


@api.get("/healthz", response={200: HealthzOut, 503: HealthzOut}, auth=None,
         tags=["health"])
def healthz(request):
    """Per-worker freshness for something outside the container to watch (§5a).

    **This read also evaluates.** §5a's external layer catches *whole container
    down* and its internal layer catches *worker died, app alive*; running the
    evaluation here means the watcher that provides the first also drives the
    second, instead of the internal layer depending on a cron entry somebody has
    to remember. Evaluation is edge-triggered, so polling this endpoint every ten
    seconds sends at most one message per stall.

    **Public, but not detailed.** Anonymous callers get liveness; thresholds and
    process internals need admin, because they are the same numbers §6a keeps out
    of the public settings response. Nothing here names a service.

    **503 when any registered worker is stale**, so a watcher can alert on the
    status code. Note that compose's healthcheck deliberately points at
    `/api/v1/services` and not here: a stalled *worker* must not make Docker
    restart `web`, which would not fix it.
    """
    deadman.evaluate()
    report = deadman.status()
    if request_is_admin(request):
        report["notifications_configured"] = notifications.is_configured()
    else:
        report["workers"] = [
            {
                "name": worker["name"],
                "state": worker["state"],
                "stale": worker["stale"],
                "last_beat_at": worker["last_beat_at"],
            }
            for worker in report["workers"]
        ]
    return (200 if report["ok"] else 503), report


@api.get("/panels", response=PanelsOut, auth=None, tags=["panels"])
def get_panels(request):
    """Everything the dashboard renders — §8's one server-side cached endpoint.

    **`auth=None` is a ruling, not a default** (operator, 2026-08-06). Node
    names, PVE versions and storage capacity are infrastructure inventory for
    hosts the catalog does not list, which is a different disclosure class from
    the catalog itself; the alternative considered was admin-scoping the two
    Proxmox panels and letting the anonymous dashboard degrade. Public won on
    the grounds that this is a tailnet-only browser home page and a dashboard
    that needs a login to be complete is a dashboard nobody uses. Recorded as an
    accepted risk in AS_BUILT §7 beside the unauthenticated-reads decision.

    What that ruling does **not** extend to, and what this endpoint therefore
    withholds:

    - **Guest names and addresses.** The nodes panel reports nodes and counts;
      the guest inventory stays server-side, as V1.4's disclosure test pinned.
    - **Why a poll failed.** `panels.py` emits a closed set of public strings;
      the snapshot's own `detail` can name environment variables and is
      admin-scoped on the settings screen.
    - **`ssl_fingerprint`**, which Proxmox returns from `/nodes` and the
      provider seam never carries.

    No outbound call happens here — this reads `provider_snapshots`, `settings`
    and `services`, which is what makes §7c's "Proxmox unreachable never blocks
    a read" structural rather than a timeout to tune.
    """
    return panels.build_panels()


@api.get("/export/json", response=list[ServiceOut], auth=None, tags=["export"])
def export_json(request):
    """On-demand full catalog dump — data portability at zero cost (§4c)."""
    return _with_policy(list(_catalog_queryset()))


@api.get("/slug-preview", response=SlugPreviewOut, auth=None, tags=["services"])
def slug_preview(request, launch_url: str = "", stack_name: str = ""):
    """Lets the UI show the derived slug without reimplementing the rule (§4c)."""
    return {"slug": derive_slug(launch_url, stack_name or None)}


@api.get("/views", response=list[SavedViewOut], auth=None, tags=["views"])
def list_views(request):
    return list(SavedView.objects.all())


# --- Writes ---------------------------------------------------------------


#: Catalog record ceiling (audit S12). A constant, not a settings key — the
#: settings table holds preferences, and "how many records before the
#: full-catalog-on-every-poll design stops holding" is an architecture bound,
#: not a preference. 2000 is ~40× the reference lab with the client-side
#: search design (§3b) still comfortable; §9 already names server-side search
#: as the successor if a catalog ever genuinely outgrows this.
MAX_SERVICES = 2000


@api.post("/services", response={200: ServiceOut, 201: ServiceOut}, auth=agent_auth,
          tags=["services"])
def upsert_service(request, payload: ServiceIn):
    """Create or update — upsert on slug (§4c).

    "An agent re-registering the same service updates it rather than
    duplicating. This is the single most important property for machine-driven
    writes; without it the catalog degrades within a week."
    """
    data = payload.dict(exclude_unset=True)
    launch_url = data.get("launch_url", "")
    slug = derive_slug(launch_url, data.pop("slug", None) or None)
    if not slug:
        raise HttpError(
            422,
            "Could not derive a slug from launch_url. A slug is the service's "
            "permanent identity and is never derived from an IP address — send "
            'an explicit "slug" (for example {"slug": "immich"}).',
        )

    service = Service.objects.filter(slug=slug).first()
    created = service is None

    if created and Service.objects.count() >= MAX_SERVICES:
        # Creates only: re-registering an existing service must keep working
        # at the ceiling, or the cap breaks the §4c idempotency the moment it
        # engages.
        raise HttpError(
            422,
            f"Catalog limit of {MAX_SERVICES} services reached. Updates still "
            "work; delete services before registering new ones.",
        )

    if created:
        service = Service(slug=slug)
        # Provenance is set by the server, never accepted from the caller (§4c).
        service.provenance = Service.Provenance.API
        service.created_by = actor_name(request)

    for attr, value in data.items():
        setattr(service, attr, value)

    if created:
        _autofill(service, data)
    if not service.name:
        # The slug is guaranteed non-empty by now and is a real identity, so it
        # beats leaving the name blank (which full_clean rejects) or falling back
        # to an address.
        service.name = _fallback_name(service.launch_url) or slug

    service.full_clean(exclude=["id"], validate_unique=False)
    try:
        with transaction.atomic():
            service.save()
    except IntegrityError:
        # Two agents registering the same NEW slug at once both saw no row,
        # and the other one's insert won (audit S13). The docstring's
        # idempotency promise means the loser must update the winner and
        # answer 200 — a 500 on the exact workload the endpoint exists for
        # (concurrent inventory reconciliation) is the one wrong answer. The
        # unique constraint is the referee; this is the recovery.
        if not created:
            raise
        service = Service.objects.filter(slug=slug).first()
        if service is None:  # not the slug collision — someone else's problem
            raise
        created = False
        # The winner's create already ran `_autofill`, and its provenance and
        # `created_by` are the truth — only the caller's payload is applied,
        # exactly as if the row had been found first time round.
        for attr, value in data.items():
            setattr(service, attr, value)
        if not service.name:
            service.name = _fallback_name(service.launch_url) or slug
        service.full_clean(exclude=["id"], validate_unique=False)
        service.save()
    schedule_static_export()
    return (201 if created else 200), service


@api.patch("/services/{slug}", response=ServiceOut, auth=agent_auth, tags=["services"])
def patch_service(request, slug: str, payload: ServicePatch):
    service = get_object_or_404(Service, slug=slug)
    for attr, value in payload.dict(exclude_unset=True).items():
        setattr(service, attr, value)
    service.full_clean(exclude=["id"], validate_unique=False)
    service.save()
    schedule_static_export()
    return service


@api.delete("/services/{slug}", response=MessageOut, auth=admin_auth, tags=["services"])
def delete_service(request, slug: str):
    """Admin scope only (§4c): an agent that can register services must not be
    able to unregister the catalog."""
    require_recent_admin_auth(request)
    service = get_object_or_404(Service, slug=slug)
    service.delete()
    schedule_static_export()
    return {"detail": f"Deleted {slug}."}


@api.post("/views", response={201: SavedViewOut}, auth=agent_auth, tags=["views"])
def create_view(request, payload: SavedViewIn):
    data = payload.dict(exclude_unset=True)
    # Audit S12, found by this window's gate: the service columns gained
    # bounds and this table — also agent-writable, also served anonymously —
    # had none.
    if len(data["name"]) > SavedView.MAX_NAME_LEN:
        raise HttpError(422, f"name: at most {SavedView.MAX_NAME_LEN} characters.")
    if len(data["query"]) > SavedView.MAX_QUERY_LEN:
        raise HttpError(422, f"query: at most {SavedView.MAX_QUERY_LEN} characters.")
    view, _ = SavedView.objects.update_or_create(
        name=data["name"],
        defaults={
            "query": data["query"],
            "position": data.get("position", 0),
        },
    )
    return 201, view


@api.patch("/views/{name}", response=SavedViewOut, auth=agent_auth, tags=["views"])
def rename_view(request, name: str, payload: SavedViewRename):
    """Rename a saved view.

    **Agent scope**, matching create and following the same reasoning as
    `rename_category`: anyone who can `POST /views` and `DELETE /views/{name}`
    can already achieve a rename in two calls, so requiring admin here would buy
    no safety while making the obvious one-call path the privileged one.

    **A name collision is refused rather than merged.** Renaming a *category*
    onto an existing one merges them, and that is right — two spellings of one
    idea should become one. Two views are two different stored queries, and
    silently discarding one of them is data loss, so this answers 422 instead.

    **The query is deliberately not editable here.** A saved view *is* its query
    (§4b: "a saved view is a stored query string, nothing more"), so changing it
    is not an edit to a label, it is replacing the view — which the existing
    create path already expresses, and which keeps this endpoint from becoming
    the second way to write a query.
    """
    view = get_object_or_404(SavedView, name=name)
    target = payload.name.strip()
    if not target:
        raise HttpError(422, "View name cannot be empty.")
    if len(target) > SavedView.MAX_NAME_LEN:
        raise HttpError(422, f"name: at most {SavedView.MAX_NAME_LEN} characters.")
    if target != name and SavedView.objects.filter(name=target).exists():
        raise HttpError(
            422, f"A view called '{target}' already exists. Pick another name."
        )
    view.name = target
    view.save(update_fields=["name"])
    return view


@api.delete("/views/{name}", response=MessageOut, auth=admin_auth, tags=["views"])
def delete_view(request, name: str):
    require_recent_admin_auth(request)
    view = get_object_or_404(SavedView, name=name)
    view.delete()
    return {"detail": f"Deleted view {name}."}


# --- Probe (admin scope only, §4c) ----------------------------------------


@api.post("/probe", response=ProbeOut, auth=admin_only_auth, tags=["probe"])
def probe(request, payload: ProbeIn):
    result = probe_url(payload.url)
    identity = _identity(result)
    return {
        "ok": result.ok,
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "title": result.title,
        "icon_url": result.icon_url,
        "redirects": result.redirects,
        "error": result.error,
        # Both may be blank when the URL is an IP and the probe found nothing —
        # the user is asked rather than handed the address.
        "suggested_slug": _suggest_slug(payload.url, identity),
        "suggested_name": identity or _fallback_name(payload.url) or None,
        # Cache read, never a provider call — the probe is already the slowest
        # thing on this path and Proxmox being down must not make it slower
        # still (§7c).
        **_derived_location(payload.url),
    }


def _derived_location(url: str) -> dict:
    preview = enrichment.preview_location(url)
    return {
        "derived_location": preview["location"],
        "derived_location_provider": preview["provider"],
    }


# --- Taxonomy maintenance ---------------------------------------------------
#
# §6b says categories and tags need no administration because "a value exists
# because a service uses it and disappears when none do". That covers their
# lifecycle but not a typo: correcting `infastructure` otherwise means editing
# every service that carries it, one at a time, and nothing surfaces the
# mistake. These endpoints are a bulk edit, not an administration screen — §5's
# "no settings screen" still holds, and no table was added.


@api.patch("/categories/{name}", response=TaxonomyOut, auth=agent_auth,
           tags=["taxonomy"])
def rename_category(request, name: str, payload: TaxonomyRename):
    """Rename a category across every service using it.

    Agent scope, because an agent could already achieve this by PATCHing each
    service; requiring admin would buy no safety.

    Renaming onto an existing category merges them, which is what you want when
    the typo and the correct value both exist.
    """
    target = payload.name.strip()
    if not target:
        raise HttpError(422, "New category name cannot be empty.")
    if len(target) > Service.MAX_NAME_LEN:
        # Audit S12, this window's gate: this bulk write lands on every
        # service via `save(update_fields=...)`, which never runs `clean()` —
        # so without its own bound it was a third path around the new
        # ceilings, agent-scoped like the others.
        raise HttpError(422, f"name: at most {Service.MAX_NAME_LEN} characters.")
    affected = list(Service.objects.filter(category=name))
    for service in affected:
        service.category = target
        service.save(update_fields=["category", "updated_at"])
    if affected:
        schedule_static_export()
    return {"name": target, "affected": len(affected)}


@api.delete("/categories/{name}", response=TaxonomyOut, auth=admin_auth,
            tags=["taxonomy"])
def clear_category(request, name: str):
    """Unset this category everywhere. The services themselves are untouched."""
    require_recent_admin_auth(request)
    affected = list(Service.objects.filter(category=name))
    for service in affected:
        service.category = None
        service.save(update_fields=["category", "updated_at"])
    if affected:
        schedule_static_export()
    return {"name": "", "affected": len(affected)}


@api.patch("/tags/{name}", response=TaxonomyOut, auth=agent_auth, tags=["taxonomy"])
def rename_tag(request, name: str, payload: TaxonomyRename):
    """Rename a tag across every service carrying it.

    Filtering happens in Python rather than with a `tags__contains` lookup. A
    server-side array lookup became *possible* when SQLite was dropped in v1.0
    and `tags` became a plain `ArrayField`, but server-side tag filtering is
    explicitly out of scope for v1 (v1 §9), and at this scale reading the whole
    catalog is free. Left as-is deliberately, not by omission.
    """
    target = payload.name.strip()
    if not target:
        raise HttpError(422, "New tag name cannot be empty.")
    if len(target) > Service.MAX_TAG_LEN:
        # Same bypass as rename_category (audit S12, this window's gate).
        raise HttpError(422, f"name: at most {Service.MAX_TAG_LEN} characters.")
    affected = 0
    for service in Service.objects.all():
        tags = service.tags or []
        if name not in tags:
            continue
        # Preserve order and merge onto an existing tag without duplicating it.
        renamed = []
        for tag in tags:
            replacement = target if tag == name else tag
            if replacement not in renamed:
                renamed.append(replacement)
        service.tags = renamed
        service.save(update_fields=["tags", "updated_at"])
        affected += 1
    if affected:
        schedule_static_export()
    return {"name": target, "affected": affected}


@api.delete("/tags/{name}", response=TaxonomyOut, auth=admin_auth, tags=["taxonomy"])
def delete_tag(request, name: str):
    """Remove this tag from every service."""
    require_recent_admin_auth(request)
    affected = 0
    for service in Service.objects.all():
        tags = service.tags or []
        if name not in tags:
            continue
        service.tags = [t for t in tags if t != name]
        service.save(update_fields=["tags", "updated_at"])
        affected += 1
    if affected:
        schedule_static_export()
    return {"name": "", "affected": affected}


@api.post("/icon/upload", response=IconOut, auth=admin_only_auth, tags=["probe"])
def upload_icon(request, file: UploadedFile = File(...)):
    """Store an icon uploaded from the admin's machine.

    The last resort when a service publishes nothing usable. No network fetch is
    involved, so there is no SSRF surface — but the bytes are still validated by
    signature rather than by filename or the browser's Content-Type, both of
    which the client controls.
    """
    if file.size > settings.PROBE_MAX_ICON_BYTES:
        raise HttpError(
            422,
            f"That image is {file.size // 1024}KB; the limit is "
            f"{settings.PROBE_MAX_ICON_BYTES // 1024}KB.",
        )
    stored = store_icon_bytes(file.read(), file.name)
    if not stored:
        raise HttpError(
            422, "That file is not a PNG, SVG, ICO, JPEG, GIF or WEBP image."
        )
    return {"icon_url": stored}


@api.post("/services/{slug}/refetch-icon", response=IconOut, auth=admin_only_auth,
          tags=["probe"])
def refetch_service_icon(request, slug: str):
    """Re-run icon discovery against the service's own URL.

    Useful after the service comes back up, or after its icon changes — the
    original probe only happens once, at creation.
    """
    service = get_object_or_404(Service, slug=slug)
    result = probe_url(service.probe_url)
    if not result.icon_url:
        detail = result.error or "no icon found on that page"
        raise HttpError(422, f"Could not find an icon: {detail}.")
    service.icon_url = result.icon_url
    service.save(update_fields=["icon_url", "updated_at"])
    schedule_static_export()
    return {"icon_url": result.icon_url}


@api.post("/icon", response=IconOut, auth=admin_only_auth, tags=["probe"])
def fetch_icon(request, payload: IconIn):
    """Cache an icon the admin picked by hand.

    Autodiscovery misses plenty of real cases — an app behind auth, an icon at
    an unconventional path, a format we refuse. This is the manual override.

    It deliberately downloads and caches server-side rather than storing the
    remote URL on the record: that keeps the catalog free of third-party
    requests from the browser, and it is what lets the recovery export inline
    the icon as base64 and still render with everything else switched off (§9).
    Admin scope and the same SSRF validator as the probe, because it is the same
    act — fetching a user-supplied URL from inside the network.
    """
    stored = fetch_and_store_icon(payload.url.strip())
    if not stored:
        raise HttpError(
            422,
            "Could not fetch that icon. It must be a reachable http(s) PNG, SVG, "
            "ICO, JPEG, GIF or WEBP under the size limit.",
        )
    return {"icon_url": stored}


# --- Settings (v1 §4, §6a) ------------------------------------------------


def _provider_status_rows() -> list[dict]:
    """Provider health for the settings screen, **from the cache only** (§7c).

    No provider is called here. `is_configured()` reads environment variables
    and touches no network; everything else comes from the snapshot the worker
    wrote. So this endpoint cannot hang, cannot time out, and keeps working with
    Proxmox switched off or on fire.

    A configured provider that has never been polled reports `usable: false`
    with "Waiting for the first poll" rather than silently looking broken —
    which is the state a fresh deployment is in for up to one poll interval.
    """
    rows = []
    snapshots = {
        row.provider: row for row in ProviderSnapshot.objects.all()
    }
    for provider in PROVIDERS:
        configured = provider.is_configured()
        snapshot = snapshots.get(provider.name)
        guests = snapshot.guests if snapshot else []
        if not configured:
            detail, usable = "Not configured.", False
        elif snapshot is None:
            detail, usable = "Waiting for the first poll.", False
        else:
            detail, usable = snapshot.detail, snapshot.ok
        # Audit S5: `poll_provider` records why a cluster read failed while the
        # guests still worked (`snapshot.ok` stays True in that case), and until
        # this row carried it, the dashboard's "see Settings for the reason"
        # pointed at a screen that showed nothing. Admin-scoped here; /panels
        # keeps its closed public strings.
        infrastructure_detail = (
            (snapshot.payload or {}).get("infrastructure_detail", "")
            if snapshot is not None
            else ""
        )
        cached_status = (
            (snapshot.payload or {}).get("status") or {} if snapshot else {}
        )
        rows.append(
            {
                "name": provider.name,
                "configured": configured,
                "usable": usable,
                "detail": detail,
                "infrastructure_detail": infrastructure_detail,
                # From the last successful poll's cached status (audit
                # informational): what the credential can actually do, beside
                # the row that says whether it works.
                "privileges": list(cached_status.get("privileges") or []),
                "guest_count": len(guests),
                "ambiguous_address_count": len(enrichment.ambiguous_addresses(guests)),
                "last_polled_at": snapshot.fetched_at if snapshot else None,
            }
        )
    return rows


@api.get("/settings", response=SettingsOut, auth=admin_only_auth, tags=["settings"])
def get_settings(request):
    """Admin-scoped, deliberately (§6a).

    §4c's "the catalog must open with no interstitial" is an argument about the
    catalog; a settings screen is not a browser home page, so gating it costs
    nothing v0 was protecting. What this returns describes infrastructure —
    whether Discord is configured, which environment variables are referenced —
    and none of that belongs in an unauthenticated response.
    """
    values = settings_store.all_settings()
    return {
        "auth_mode": current_auth_mode(),
        "sections": list(settings_store.SECTIONS),
        "environment": settings_store.env_panel(),
        "providers": _provider_status_rows(),
        "settings": [
            {
                "key": spec.key,
                "kind": spec.kind,
                "value": values[spec.key],
                "default": spec.default,
                "label": spec.label,
                "helper": spec.helper,
                "section": spec.section,
                "unit": spec.unit,
                "minimum": spec.minimum,
                "step": spec.step,
                "choices": list(spec.choices),
                "consumer": spec.consumer,
            }
            for spec in settings_store.REGISTRY
        ],
    }


@api.patch("/settings", response=SettingsOut, auth=admin_only_auth, tags=["settings"])
def update_settings(request, payload: dict = Body(...)):
    """Write a batch of keys, atomically.

    The screen saves a form rather than a field, and a partial write leaves the
    operator looking at rows where some took and some did not, with nothing
    saying which.
    """
    try:
        settings_store.write_settings(payload)
    except settings_store.SettingRejected as exc:
        raise HttpError(422, str(exc)) from exc
    return get_settings(request)


@api.get("/settings/public", response=PublicSettingsOut, auth=None, tags=["settings"])
def get_public_settings(request):
    """The narrow public endpoint (§6a) — an explicit whitelist of display keys.

    A whitelist rather than a blacklist because the leak risk here is almost
    entirely "a generic serializer dumps every key", and a whitelist makes that
    structurally impossible instead of a thing to remember. Adding a key to the
    registry does not add it here.
    """
    return {
        "auth_mode": current_auth_mode(),
        "values": settings_store.public_settings(),
    }


# --- Session --------------------------------------------------------------


@api.post("/session", response={200: SessionOut}, auth=None, tags=["session"])
def create_session(request, payload: SessionIn):
    """Exchange the admin token for an httpOnly, SameSite=Strict cookie (§4c).

    Only the admin token is accepted: the session cookie exists for the UI, and
    the UI is admin. Agent scope stays bearer-only.
    """
    check_login_throttle(request)
    scope = scope_for_token(payload.token)
    if scope != SCOPE_ADMIN:
        record_failed_login(request)
        # Deliberately does not distinguish "wrong token" from "agent token used
        # here" — that difference is only useful to someone guessing.
        raise HttpError(401, "Invalid admin token.")
    clear_login_throttle(request)
    start_admin_session(request)
    return 200, {"scope": SCOPE_ADMIN}


@api.delete("/session", response=MessageOut, auth=admin_auth, tags=["session"])
def end_session(request):
    request.session.flush()
    return {"detail": "Signed out."}


# --- Helpers --------------------------------------------------------------


# Page titles are overwhelmingly "Name <sep> tagline": "OmniRoute — AI Gateway
# for Multi-Provider LLMs", "Grafana | Dashboards". Only the first segment
# identifies the service.
#
# The separator must have whitespace on BOTH sides. That is what distinguishes a
# separator from a hyphen inside a compound name — "Kara-Keep" and "open-webui"
# must survive intact.
# A colon is written tight against the preceding word ("Jellyfin: Media System"),
# so it only needs trailing whitespace. Requiring a space AFTER it keeps "16:9"
# and "12:30" intact.
_TITLE_SEPARATOR = re.compile(r"\s+[|·•—–]\s+|\s+-\s+|\s*:\s+")

# Segments that name a PAGE rather than the service. Probing an app that
# requires auth lands on its login screen, so "Login - Sonarr" and
# "Login - Radarr" both reduce to "Login" if you simply take the first segment —
# two services, one slug, and the later import silently overwrites the earlier.
#
# Kept deliberately tight: only words that are never a homelab service's name.
# "Portal", "Console" and "Hub" are excluded from this list because they are.
_GENERIC_TITLE_SEGMENTS = frozenset(
    {
        "login",
        "log in",
        "log on",
        "logon",
        "signin",
        "sign in",
        "sign in to continue",
        "please sign in",
        "please log in",
        "authentication",
        "authenticate",
        "unauthorized",
        "unauthorised",
        "forbidden",
        "access denied",
        "welcome",
        "home",
        "homepage",
        "index",
        "dashboard",
        "dashboards",
        "overview",
        "loading",
        "untitled",
        "error",
        "not found",
    }
)


def _primary_title(title: str | None) -> str | None:
    """The identifying part of a page title.

    Splits on the usual separators, discards segments that merely name the page,
    and takes the first of what is left. Falls back to the whole title rather
    than returning nothing.
    """
    if not title:
        return None
    cleaned = re.sub(r"\s+", " ", title).strip()
    if not cleaned:
        return None

    segments = [s.strip() for s in _TITLE_SEPARATOR.split(cleaned) if s.strip()]
    if not segments:
        return cleaned

    meaningful = [
        s for s in segments if s.casefold().strip(" .!") not in _GENERIC_TITLE_SEGMENTS
    ]
    # If every segment is generic, the title genuinely says nothing about the
    # service; hand back the first one and let the caller try another signal.
    return (meaningful or segments)[0]


def _looks_generic(title: str | None) -> bool:
    """True when a title identifies a page, not a service."""
    if not title:
        return True
    cleaned = re.sub(r"\s+", " ", title).strip().casefold().strip(" .!")
    return cleaned in _GENERIC_TITLE_SEGMENTS


def _identity(result) -> str | None:
    """Best available name for the service behind a probed page.

    Order matters: the title is the strongest signal when it says anything, but
    an auth-walled app's title describes the login screen, so fall through to the
    metadata apps publish for exactly this purpose.
    """
    primary = _primary_title(result.title)
    if primary and not _looks_generic(primary):
        return primary
    if result.site_name:
        return result.site_name
    # A short description is usually the app's own name ("Sonarr"); a long one is
    # marketing prose and useless as an identifier.
    if result.description and len(result.description) <= 40:
        return result.description.strip()
    return primary


def _bounded_slug(value: str, limit: int = 32) -> str:
    """Keep a derived slug short enough to read in a table and a URL.

    Truncates at a hyphen boundary so the result is still whole words.
    """
    if len(value) <= limit:
        return value
    cut = value[:limit]
    if "-" in cut:
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-") or value[:limit].strip("-")


def _suggest_slug(launch_url: str, identity: str | None) -> str:
    """Slug the UI offers as a default. A SUGGESTION ONLY.

    Deliberately not used by `upsert_service`, which derives the slug purely
    from the URL and any supplied stack name. Server-side derivation must be
    deterministic: if it consulted the page title, registering a service while
    it is up would yield `openobserve` and re-registering while it is down
    would yield `192-168-50-17` — two records for one service, the exact
    failure the upsert exists to prevent.

    Here, with a human looking at the result and free to edit it, a title beats
    an address. The slug is immutable after creation (§4), so an IP-derived one
    permanently encodes where the service happened to live on the day it was
    added.
    """
    host = urlsplit(
        launch_url if "//" in launch_url else f"//{launch_url}"
    ).hostname
    if host and is_ip_literal(host) and identity and not _looks_generic(identity):
        from_identity = derive_slug("", stack_name=identity)
        if from_identity:
            return _bounded_slug(from_identity)
    return derive_slug(launch_url)


def _fallback_name(launch_url: str) -> str:
    """Display name when the probe yields nothing usable.

    Returns "" for an IP-addressed URL: "192.168.50.89" is not a name, and
    showing it as one invites the user to accept it. Blank means the UI asks.
    """
    host = urlsplit(launch_url if "//" in launch_url else f"//{launch_url}").hostname
    if not host or is_ip_literal(host):
        return ""
    label = host.split(".")[0]
    return label.replace("-", " ").replace("_", " ").title()


def _guess_category(*hints: str | None) -> str | None:
    haystack = " ".join(h.lower() for h in hints if h)
    for category, keywords in _CATEGORY_HINTS.items():
        if any(keyword in haystack for keyword in keywords):
            return category
    return None


def _autofill(service: Service, supplied: dict) -> None:
    """Probe internally on create to derive name/icon/category (§4c: "POST
    /api/v1/services probes internally as part of an authorized create").

    Best-effort: a create must never fail because the service happens to be
    down. An unreachable URL is a perfectly valid catalog entry.
    """
    needs = not supplied.get("name") or not supplied.get("icon_url")
    if not needs:
        return
    try:
        validate_url(service.probe_url)
    except ProbeRejected as exc:
        logger.info("Skipping autofill probe for %s: %s", service.slug, exc)
        return
    try:
        result = probe_url(service.probe_url)
    except Exception:  # noqa: BLE001 - autofill must never break a write
        logger.exception("Autofill probe failed for %s", service.slug)
        return
    if not result.ok:
        return
    if not supplied.get("name"):
        identity = _identity(result)
        if identity:
            service.name = identity[:120]
    if not supplied.get("icon_url") and result.icon_url:
        service.icon_url = result.icon_url
    if not supplied.get("category"):
        service.category = _guess_category(service.name, service.launch_url)
