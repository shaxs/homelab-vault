"""Dashboard panels — v1 §8, §7d.

**§8's one architectural requirement is that panels are assembled here and not
in the browser**: *"panels fetch server-side, cache, and expose through an
endpoint … a panel that fetches from the frontend gets rebuilt."* §8 calls it
the one v1 decision v2 depends on, because widgets consume the same pipe.

So this module is deliberately its own file, importing **no HTTP client and no
provider**, exactly as `enrichment.py` is kept apart from `poll.py`. The
guarantee that a dashboard read cannot hang on Proxmox is readable off the
import list rather than resting on a timeout somebody has to keep correct.

**Two things this module does not do, both on purpose.**

*It does not format.* Bytes stay bytes and seconds stay seconds; the browser
turns them into `2.21 TiB` and `24d 03:12`. A v2 widget consuming this pipe
wants the numbers, and a percentage computed twice in two places is a
percentage that will disagree once.

*It does not disclose why a poll failed.* The panel payload is **public** by
operator ruling (2026-08-06), while the snapshot's own `detail` is written for
the admin-scoped settings screen and can name environment variables or quote a
`pveum` command containing the token's `user@realm`. `_unavailable()` is the
only text this module emits, and it is a closed set — see the security note
there.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from . import settings_store
from .health.policy import current_policy
from .models import ProviderSnapshot, Service
from .providers import PROVIDERS
from .providers.base import CRITICAL_ALERT_LEVELS

#: Panel keys, in the order the dashboard renders them. The status summary is
#: first because it is the only panel that works with no provider configured —
#: §7d's "not configured means not rendered" has to leave a usable page behind.
#: The four TrueNAS keys are sub-blocks of the TrueNAS adapter section, each
#: gated by its own `ui.panels.truenas_*` toggle exactly as the Proxmox pair are.
PANEL_STATUS_SUMMARY = "status_summary"
PANEL_PROXMOX_CLUSTER = "proxmox_cluster"
PANEL_PROXMOX_NODES = "proxmox_nodes"
PANEL_TRUENAS_SYSTEM = "truenas_system"
PANEL_TRUENAS_POOLS = "truenas_pools"
PANEL_TRUENAS_DISKS = "truenas_disks"
PANEL_TRUENAS_ALERTS = "truenas_alerts"
PANEL_ORDER = (
    PANEL_STATUS_SUMMARY,
    PANEL_PROXMOX_CLUSTER,
    PANEL_PROXMOX_NODES,
    PANEL_TRUENAS_SYSTEM,
    PANEL_TRUENAS_POOLS,
    PANEL_TRUENAS_DISKS,
    PANEL_TRUENAS_ALERTS,
)

#: The providers §7d's panels describe. Named once so a second provider with a
#: nodes panel is a change here rather than a string search.
PROXMOX = "proxmox"
TRUENAS = "truenas"

#: The pseudo-adapter that leads the bar — a curated cross-adapter summary, not a
#: provider. Carries no health dot (TN4 design).
OVERVIEW = "overview"

#: Adapter bar order and the human label each pill shows. Only *configured*
#: providers get a pill; Overview is prepended unconditionally.
ADAPTER_ORDER = (PROXMOX, TRUENAS)
ADAPTER_LABELS = {PROXMOX: "Proxmox", TRUENAS: "TrueNAS"}

#: Pool statuses that mean "something is wrong" for the adapter health dot.
_POOL_BAD = frozenset({"DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"})

# --- Public unavailability reasons -----------------------------------------
#
# A closed set. Nothing derived from a provider response, an exception message
# or an environment variable ever reaches an anonymous caller; the operator
# reads the real reason on the admin-scoped settings screen, which V1.4 built
# for exactly that purpose.
WAITING = "Waiting for the first poll."
POLLING_OFF = "Proxmox polling is switched off in Settings."
UNAVAILABLE = "Cluster state is unavailable — see Settings for the reason."

# The TrueNAS equivalents — same closed-set discipline. A poll failure's real
# reason (which can name TRUENAS_HOST or quote an httpx error) stays on the
# admin-scoped settings screen; the public panel gets one of these three.
TN_WAITING = "Waiting for the first poll."
TN_POLLING_OFF = "TrueNAS polling is switched off in Settings."
TN_UNAVAILABLE = "TrueNAS state is unavailable — see Settings for the reason."


def visible_panels() -> list[str]:
    """Which panels the operator has switched on (§8's escape valve).

    Visibility is applied **here**, server-side, and a hidden panel's payload is
    omitted from the response entirely rather than sent and hidden by CSS. That
    makes the setting a real boundary: a hidden Proxmox panel discloses nothing,
    which would not be true of a frontend that merely declines to draw it.
    """
    values = settings_store.all_settings()
    return [key for key in PANEL_ORDER if values.get(f"ui.panels.{key}")]


def build_panels() -> dict:
    """Everything the dashboard renders, from cached state only."""
    panels = visible_panels()
    values = settings_store.all_settings()
    # Both provider snapshots in one read, shared by the adapter bar, the curated
    # overview and the TrueNAS section — three consumers, one query, one
    # `fetched_at` per provider to reason about staleness with.
    snapshots = {
        row.provider: row
        for row in ProviderSnapshot.objects.filter(provider__in=(PROXMOX, TRUENAS))
    }
    return {
        "landing_page": values.get("ui.landing_page", "catalog"),
        "panels": panels,
        # The adapter bar (TN4): Overview + one pill per configured provider,
        # each with a health dot. Always present, so an unconfigured lab still
        # renders a bar with just Overview.
        "adapters": _adapters(values, snapshots),
        # The curated cross-adapter summary the Overview pill shows. Always
        # present — it works with no provider configured, like the status panel.
        "overview": _overview(values, snapshots),
        "status_summary": (
            _status_summary() if PANEL_STATUS_SUMMARY in panels else None
        ),
        "proxmox": _proxmox(panels, values),
        "truenas": _truenas(panels, values, snapshots.get(TRUENAS)),
    }


def _provider(name: str):
    """The registered provider with this name, or `None`."""
    return next((p for p in PROVIDERS if p.name == name), None)


def _is_stale(fetched_at, interval_s) -> bool:
    """3x the poll interval, matching the deadman's rule (§5a) rather than
    inventing a second definition of "too old to trust". Used by every panel and
    every adapter health dot so they cannot disagree about staleness."""
    return timezone.now() - fetched_at > timedelta(seconds=3 * int(interval_s))


#: How many services the attention list names before it says "and N more".
#: A panel is a glance, and a list that can be 46 rows long is a second catalog.
#: Operator ruling 2026-08-07: 5, down from 8 — with a real catalog and a stopped
#: worker, eight rows already dominated the panel.
ATTENTION_LIMIT = 5

#: Worst first. `off` is absent deliberately: checks being switched off is a
#: decision somebody made, not a thing demanding attention.
ATTENTION_ORDER = ("down", "stale", "unknown")


def _status_summary() -> dict:
    """Counts of every status the catalog can show, plus what is wrong (§8).

    Uses `Service.effective_status()` — the same derivation the catalog
    serialises — rather than counting the stored `health_status` column, so the
    dashboard and the catalog cannot disagree about how many services are
    `stale`. Counting the column would be simpler, faster, and wrong: `stale`
    and `off` are never stored.

    **The attention list is served from here rather than assembled in the
    browser**, even though the catalog is already loaded client-side, because
    §8's requirement is that a panel's data comes from this endpoint. Deriving
    the same six numbers a second way in the frontend is how the dashboard and
    the catalog end up disagreeing by one service and nobody can say which is
    right.
    """
    now = timezone.now()
    policy = current_policy()
    tally = {"up": 0, "down": 0, "stale": 0, "unknown": 0, "off": 0}
    total = 0
    attention: dict[str, list[dict]] = {key: [] for key in ATTENTION_ORDER}
    # `.only()` is not micro-optimisation here (api-security gate, API4). This
    # endpoint is public and the dashboard polls it every 15 seconds, while
    # `icon_url` can hold a base64 data URI — `all()` would pull every icon in
    # the catalog on every poll to count six numbers.
    columns = Service.objects.only(
        "slug",
        "name",
        "health_enabled",
        "health_status",
        "health_checked_at",
        "health_interval_s",
    )
    for service in columns.order_by("name"):
        total += 1
        status = service.effective_status(now=now, policy=policy)
        if status in tally:
            tally[status] += 1
        if status in attention:
            attention[status].append(
                {"slug": service.slug, "name": service.name, "status": status}
            )
    ranked = [row for key in ATTENTION_ORDER for row in attention[key]]
    return {
        "total": total,
        **tally,
        "attention": ranked[:ATTENTION_LIMIT],
        "attention_total": len(ranked),
    }


def _proxmox(panels: list[str], values: dict) -> dict | None:
    """The two Proxmox panels, or `None` when neither should render.

    `None` covers §7d's *"not configured means not rendered"* and the case where
    the operator switched both panels off. Everything else renders the panel
    **with a reason in it** — an operator whose cluster stopped answering needs
    to see that the panel exists and is empty, not to watch it vanish.
    """
    wanted = [key for key in (PANEL_PROXMOX_CLUSTER, PANEL_PROXMOX_NODES) if key in panels]
    if not wanted:
        return None

    provider = _provider(PROXMOX)
    # `is_configured()` reads environment variables and touches no network,
    # which is what makes it safe on a request path.
    if provider is None or not provider.is_configured():
        return None

    snapshot = ProviderSnapshot.objects.filter(provider=PROXMOX).first()
    interval_s = values.get("proxmox.poll_interval_s") or 120
    payload = {
        "available": False,
        "detail": WAITING,
        "polled_at": snapshot.fetched_at if snapshot else None,
        "stale": False,
        "cluster": None,
        "nodes": None,
    }

    if not values.get("proxmox.polling.enabled"):
        # A frozen panel is worse than an honest empty one: with polling off the
        # numbers stop moving and nothing on screen would say why.
        payload["detail"] = POLLING_OFF
        return payload
    if snapshot is None:
        return payload
    if not snapshot.ok:
        payload["detail"] = UNAVAILABLE
        return payload

    infrastructure = (snapshot.payload or {}).get("infrastructure")
    if not infrastructure:
        payload["detail"] = UNAVAILABLE
        return payload

    payload["available"] = True
    payload["detail"] = ""
    payload["stale"] = _is_stale(snapshot.fetched_at, interval_s)
    if PANEL_PROXMOX_CLUSTER in wanted:
        payload["cluster"] = _cluster(infrastructure, snapshot.guests)
    if PANEL_PROXMOX_NODES in wanted:
        payload["nodes"] = infrastructure.get("nodes") or []
    return payload


def _cluster(infrastructure: dict, guests: list) -> dict:
    """§7d's summary row: cluster health, compute, storage.

    Compute totals are aggregated across **online** nodes only — an offline node
    contributes no capacity, and including its cores would report a cluster with
    more CPU than it can actually run anything on.

    The CPU percentage is **weighted by core count**, not averaged across nodes.
    A 4-core node at 90% and a 20-core node at 5% is not a cluster at 47.5%.
    """
    nodes = infrastructure.get("nodes") or []
    online = [node for node in nodes if node.get("online")]

    cpu_count = sum(node.get("cpu_count") or 0 for node in online)
    cpu_weighted = sum(
        (node.get("cpu_pct") or 0) * (node.get("cpu_count") or 0) for node in online
    )
    mem_used = sum(node.get("mem_used_bytes") or 0 for node in online)
    mem_total = sum(node.get("mem_total_bytes") or 0 for node in online)

    return {
        "name": infrastructure.get("cluster_name") or "",
        "quorate": infrastructure.get("quorate"),
        "ha": infrastructure.get("ha") or "unknown",
        "nodes_online": len(online),
        "nodes_total": len(nodes),
        "cpu_pct": round(cpu_weighted / cpu_count, 1) if cpu_count else None,
        "cpu_count": cpu_count,
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        "mem_pct": _pct(mem_used, mem_total),
        "storages_online": infrastructure.get("storages_online") or 0,
        "storages_total": infrastructure.get("storages_total") or 0,
        "storage_used_bytes": infrastructure.get("storage_used_bytes") or 0,
        "storage_total_bytes": infrastructure.get("storage_total_bytes") or 0,
        "storage_pct": _pct(
            infrastructure.get("storage_used_bytes") or 0,
            infrastructure.get("storage_total_bytes") or 0,
        ),
        # From the cached guest list rather than a second cluster call: the
        # guests are already here and counting them twice invites two answers.
        "vm_count": sum(1 for guest in guests if guest.get("kind") == "qemu"),
        "lxc_count": sum(1 for guest in guests if guest.get("kind") == "lxc"),
    }


def _pct(used: int, total: int) -> float | None:
    return round(100 * used / total, 1) if total else None


# --- The adapter bar (TN4) -------------------------------------------------


def _adapters(values: dict, snapshots: dict) -> list[dict]:
    """The pill strip: an `Overview` pill plus one per CONFIGURED provider.

    Built from `is_configured()` (env-only, no network — safe on a request path),
    so an unconfigured provider yields no pill and the operator never sees an
    adapter they have not set up. Overview leads and carries no health dot; each
    provider pill carries `ok`/`warn`/`err` derived from its cached snapshot, so a
    degraded adapter is visible without opening it. The Workspaces concept — the
    `+` tab, catalogued-service pills, tab counts — is deliberately absent (out of
    v1.2 scope, operator ruling).
    """
    adapters = [{"key": OVERVIEW, "label": "Overview", "health": ""}]
    for name in ADAPTER_ORDER:
        provider = _provider(name)
        if provider is None or not provider.is_configured():
            continue
        adapters.append(
            {
                "key": name,
                "label": ADAPTER_LABELS[name],
                "health": _adapter_health(name, values, snapshots.get(name)),
            }
        )
    return adapters


def _adapter_health(name: str, values: dict, snapshot) -> str:
    if name == PROXMOX:
        return _proxmox_health(values, snapshot)
    if name == TRUENAS:
        return _truenas_health(values, snapshot)
    return ""


def _proxmox_health(values: dict, snapshot) -> str:
    """A closed `ok`/`warn`/`err` for the Proxmox pill, from cache only.

    `warn` covers the "nothing to see yet or moving on" states (polling off,
    awaiting first poll, stale, a node offline); `err` is reserved for a failed
    poll, a missing cluster read, or a lost quorum — the states an operator
    should act on.
    """
    if not values.get("proxmox.polling.enabled"):
        return "warn"
    if snapshot is None:
        return "warn"
    if not snapshot.ok:
        return "err"
    infrastructure = (snapshot.payload or {}).get("infrastructure")
    if not infrastructure:
        return "err"
    interval_s = values.get("proxmox.poll_interval_s") or 120
    if _is_stale(snapshot.fetched_at, interval_s):
        return "warn"
    if infrastructure.get("quorate") is False:
        return "err"
    nodes = infrastructure.get("nodes") or []
    if any(not node.get("online") for node in nodes):
        return "warn"
    return "ok"


def _truenas_health(values: dict, snapshot) -> str:
    """A closed `ok`/`warn`/`err` for the TrueNAS pill, from cache only.

    `err` is the loud state the whole feature exists for — a DEGRADED/FAULTED
    pool or an active critical native alert (the disk that degraded for six
    months undetected). `warn` is polling off, awaiting a poll, stale, or a
    non-critical alert standing.
    """
    if not values.get("truenas.polling.enabled"):
        return "warn"
    if snapshot is None:
        return "warn"
    if not snapshot.ok:
        return "err"
    report = (snapshot.payload or {}).get("report")
    if not report:
        return "err"
    interval_s = values.get("truenas.poll_interval_s") or 120
    if _is_stale(snapshot.fetched_at, interval_s):
        return "warn"
    pools = report.get("pools") or []
    if any(
        not pool.get("healthy") or (pool.get("status") or "") in _POOL_BAD
        for pool in pools
    ):
        return "err"
    alerts = report.get("alerts") or []
    if any((alert.get("level") or "") in CRITICAL_ALERT_LEVELS for alert in alerts):
        return "err"
    if alerts:
        return "warn"
    return "ok"


# --- The TrueNAS adapter section (TN3) -------------------------------------


def _truenas(panels: list[str], values: dict, snapshot) -> dict | None:
    """The TrueNAS section, or `None` when it should not render.

    Mirrors `_proxmox` exactly: `None` for §7d's "not configured means not
    rendered" and for the case where all four TrueNAS panels are switched off.
    Everything else renders **with a reason in it** — a box that stopped
    answering should show an empty section that says why, not vanish.

    Each of the four sub-blocks is gated by its own toggle: a switched-off
    sub-block is **absent from the payload**, not sent and hidden client-side, so
    a hidden panel discloses nothing (§8's escape valve, held server-side).
    `backups` has no toggle in the spec's four and rides along whenever the
    section is available.
    """
    wanted = [
        key
        for key in (
            PANEL_TRUENAS_SYSTEM,
            PANEL_TRUENAS_POOLS,
            PANEL_TRUENAS_DISKS,
            PANEL_TRUENAS_ALERTS,
        )
        if key in panels
    ]
    if not wanted:
        return None

    provider = _provider(TRUENAS)
    if provider is None or not provider.is_configured():
        return None

    interval_s = values.get("truenas.poll_interval_s") or 120
    payload = {
        "available": False,
        "detail": TN_WAITING,
        "polled_at": snapshot.fetched_at if snapshot else None,
        "stale": False,
        "hostname": "",
        "system": None,
        "pools": None,
        "disks": None,
        "backups": None,
        "alerts": None,
    }

    if not values.get("truenas.polling.enabled"):
        payload["detail"] = TN_POLLING_OFF
        return payload
    if snapshot is None:
        return payload
    if not snapshot.ok:
        payload["detail"] = TN_UNAVAILABLE
        return payload

    report = (snapshot.payload or {}).get("report")
    if not report:
        payload["detail"] = TN_UNAVAILABLE
        return payload

    payload["available"] = True
    payload["detail"] = ""
    payload["stale"] = _is_stale(snapshot.fetched_at, interval_s)
    system = report.get("system") or {}
    payload["hostname"] = system.get("hostname") or ""
    # backups are not one of the four toggles — always present when available.
    payload["backups"] = [
        _truenas_backup(task) for task in (report.get("backups") or [])
    ]
    if PANEL_TRUENAS_SYSTEM in wanted:
        payload["system"] = _truenas_system(system)
    if PANEL_TRUENAS_POOLS in wanted:
        payload["pools"] = [_truenas_pool(pool) for pool in (report.get("pools") or [])]
    if PANEL_TRUENAS_DISKS in wanted:
        payload["disks"] = [_truenas_disk(disk) for disk in (report.get("disks") or [])]
    if PANEL_TRUENAS_ALERTS in wanted:
        payload["alerts"] = [
            _truenas_alert(alert) for alert in (report.get("alerts") or [])
        ]
    return payload


def _truenas_system(system: dict) -> dict:
    """The System card: version, uptime, memory and load. Same disclosure class
    as the Proxmox node data already public here — no serials, no free text."""
    used = system.get("mem_used_bytes")
    total = system.get("mem_total_bytes")
    return {
        "version": system.get("version") or "",
        "hostname": system.get("hostname") or "",
        "uptime_s": system.get("uptime_s"),
        "mem_used_bytes": used,
        "mem_total_bytes": total,
        "mem_pct": _pct(used, total) if used is not None and total else None,
        "load_avg": system.get("load_avg"),
        "cores": system.get("cores"),
    }


def _truenas_pool(pool: dict) -> dict:
    """One pool row, with the used-percentage computed server-side (never twice
    in two places, so it cannot disagree)."""
    used = pool.get("used_bytes")
    total = pool.get("total_bytes")
    return {
        "name": pool.get("name") or "",
        "status": pool.get("status") or "",
        "healthy": bool(pool.get("healthy")),
        "used_bytes": used,
        "total_bytes": total,
        "free_bytes": pool.get("free_bytes"),
        "used_pct": _pct(used, total) if used is not None and total else None,
        "scan_function": pool.get("scan_function") or "",
        "scan_state": pool.get("scan_state") or "",
        "scan_percentage": pool.get("scan_percentage"),
    }


def _truenas_disk(disk: dict) -> dict:
    """One disk row. Whitelisted fields only — **no serial** (the `Disk`
    dataclass omits it, and re-whitelisting here is defence in depth)."""
    return {
        "name": disk.get("name") or "",
        "model": disk.get("model") or "",
        "disk_type": disk.get("disk_type") or "",
        "size_bytes": disk.get("size_bytes"),
        "pool": disk.get("pool") or "",
        "temperature_c": disk.get("temperature_c"),
        "smart_status": disk.get("smart_status") or "",
    }


def _truenas_backup(task: dict) -> dict:
    """One backup-task row: kind, name, last-run state and time."""
    return {
        "kind": task.get("kind") or "",
        "name": task.get("name") or "",
        "state": task.get("state") or "",
        "at": task.get("at"),
    }


def _truenas_alert(alert: dict) -> dict:
    """A native alert, **coarsened for the public panel** (TN1's obligation).

    The raw `formatted` message names pools/datasets and is attacker-influenceable
    free text, so it is dropped here entirely — the public row is `level` +
    `category` + time. `category` is TrueNAS's `klass` (a provider type
    identifier like `SMARTError`, carrying no such content), which is what the
    dashboard shows instead. The raw text stays cached for the `sanitize()`d
    Discord path and never reaches this endpoint. A disclosure test pins it.
    """
    return {
        "level": alert.get("level") or "",
        "category": alert.get("category") or "",
        "at": alert.get("at"),
    }


# --- The curated Overview (TN4) --------------------------------------------


def _overview(values: dict, snapshots: dict) -> dict:
    """A couple of headline stats per configured adapter, from cache only.

    Deliberately a curated glance rather than raw section data: the values here
    are display-ready summary strings (`"3 / 4"`, `"Healthy"`, `"72%"`), because
    the Overview tiles are a summary the operator scans, not the numeric pipe a
    v2 widget consumes — that is the section panels above, which keep raw
    numbers. Each adapter appears only when configured; the tile drills into its
    section client-side.
    """
    adapters = []
    if (provider := _provider(PROXMOX)) is not None and provider.is_configured():
        adapters.append(_overview_proxmox(values, snapshots.get(PROXMOX)))
    if (provider := _provider(TRUENAS)) is not None and provider.is_configured():
        adapters.append(_overview_truenas(values, snapshots.get(TRUENAS)))
    return {"adapters": adapters}


def _overview_proxmox(values: dict, snapshot) -> dict:
    health = _proxmox_health(values, snapshot)
    infrastructure = (
        (snapshot.payload or {}).get("infrastructure")
        if snapshot and snapshot.ok
        else None
    )
    stats = []
    if infrastructure:
        nodes = infrastructure.get("nodes") or []
        online = sum(1 for node in nodes if node.get("online"))
        stats.append(
            {
                "label": "Nodes online",
                "value": f"{online} / {len(nodes)}",
                "tone": "ok" if nodes and online == len(nodes) else "warn",
            }
        )
        if infrastructure.get("quorate") is False:
            cluster, tone = "No quorum", "err"
        elif any(not node.get("online") for node in nodes):
            cluster, tone = "Degraded", "warn"
        else:
            cluster, tone = "Healthy", "ok"
        stats.append({"label": "Cluster", "value": cluster, "tone": tone})
    else:
        stats.append({"label": "Cluster", "value": "Unavailable", "tone": "err"})
    return {"key": PROXMOX, "label": "Proxmox", "health": health, "stats": stats}


def _overview_truenas(values: dict, snapshot) -> dict:
    health = _truenas_health(values, snapshot)
    report = (
        (snapshot.payload or {}).get("report") if snapshot and snapshot.ok else None
    )
    stats = []
    if report:
        pools = report.get("pools") or []
        healthy = sum(1 for pool in pools if pool.get("healthy"))
        stats.append(
            {
                "label": "Pools healthy",
                "value": f"{healthy} / {len(pools)}",
                "tone": "ok" if pools and healthy == len(pools) else "err",
            }
        )
        used = sum(
            pool.get("used_bytes") or 0
            for pool in pools
            if pool.get("used_bytes") and pool.get("total_bytes")
        )
        total = sum(
            pool.get("total_bytes") or 0
            for pool in pools
            if pool.get("used_bytes") and pool.get("total_bytes")
        )
        pct = _pct(used, total) if total else None
        stats.append(
            {
                "label": "Capacity used",
                "value": f"{pct}%" if pct is not None else "—",
                "tone": "warn" if pct is not None and pct >= 90 else "ok",
            }
        )
        critical = sum(
            1
            for alert in (report.get("alerts") or [])
            if (alert.get("level") or "") in CRITICAL_ALERT_LEVELS
        )
        stats.append(
            {
                "label": "Critical alerts",
                "value": str(critical),
                "tone": "err" if critical else "ok",
            }
        )
    else:
        stats.append({"label": "State", "value": "Unavailable", "tone": "err"})
    return {"key": TRUENAS, "label": "TrueNAS", "health": health, "stats": stats}
