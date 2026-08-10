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

#: Panel keys, in the order the dashboard renders them. The status summary is
#: first because it is the only panel that works with no provider configured —
#: §7d's "not configured means not rendered" has to leave a usable page behind.
PANEL_STATUS_SUMMARY = "status_summary"
PANEL_PROXMOX_CLUSTER = "proxmox_cluster"
PANEL_PROXMOX_NODES = "proxmox_nodes"
PANEL_ORDER = (PANEL_STATUS_SUMMARY, PANEL_PROXMOX_CLUSTER, PANEL_PROXMOX_NODES)

#: The provider §7d's panels describe. Named once so a second provider with a
#: nodes panel is a change here rather than a string search.
PROXMOX = "proxmox"

# --- Public unavailability reasons -----------------------------------------
#
# A closed set. Nothing derived from a provider response, an exception message
# or an environment variable ever reaches an anonymous caller; the operator
# reads the real reason on the admin-scoped settings screen, which V1.4 built
# for exactly that purpose.
WAITING = "Waiting for the first poll."
POLLING_OFF = "Proxmox polling is switched off in Settings."
UNAVAILABLE = "Cluster state is unavailable — see Settings for the reason."


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
    return {
        "landing_page": values.get("ui.landing_page", "catalog"),
        "panels": panels,
        "status_summary": (
            _status_summary() if PANEL_STATUS_SUMMARY in panels else None
        ),
        "proxmox": _proxmox(panels, values),
    }


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

    provider = next((p for p in PROVIDERS if p.name == PROXMOX), None)
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
    # 3x the interval, matching the deadman's rule (§5a) rather than inventing a
    # second definition of "too old to trust".
    payload["stale"] = timezone.now() - snapshot.fetched_at > timedelta(
        seconds=3 * int(interval_s)
    )
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
