"""The provider contract — v1 §7a.

Four methods, deliberately: enumerate guests, describe the provider's own
health, turn a guest into a location string, and — added in V1.5 for §7d's
nodes panel — describe the cluster those guests run on. Everything else —
matching, persistence, refresh-by-foreign-id — belongs to the enrichment path
and is written once, against this interface, so a second provider changes none
of it.

**`infrastructure()` is the one method a provider may legitimately not have an
answer for**, and it returns `None` rather than an empty `Infrastructure` in
that case, so "this provider has no cluster" stays distinguishable from "this
cluster has no nodes".

**Why `ProviderStatus` carries three booleans and not one.** `is_configured()`
answers "are there credentials", which is the question §7d asks ("no token ->
no panel, no enrichment, no worker"). It is not enough on its own, because
there is a third state between *not configured* and *working*: **configured but
blind.** Proxmox filters collections by permission and returns `200 []` rather
than 403, so a token with no ACL enumerates zero guests through an entirely
successful-looking call. A provider that reported that as "no guests" would
write no links, forever, while every request returned 200 and the operator saw
"Proxmox is configured and my services never get a location."

Found against the live cluster on 2026-08-06 before any of this existed, which
is why the state is in the contract rather than bolted onto one implementation:
any provider fronting a permissioned API can be blind the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class ProviderUnavailable(Exception):
    """The provider could not be reached or refused the request.

    Raised out of `guests()` and `status()` only. **No caller on a request path
    may let this escape** — §7c requires that the provider being unreachable
    never blocks a catalog write or a probe, and the structural guarantee is
    that request paths read the cached snapshot instead of calling a provider
    at all. This exception belongs to the worker.
    """


@dataclass(frozen=True)
class Guest:
    """One VM or container, as a provider sees it.

    `foreign_id` is the stable identity — Proxmox's VMID — and is what links are
    refreshed by (§7b). The address is explicitly *not* identity: it moves with
    DHCP and NIC changes, so it is used to match a service once and never again.
    """

    foreign_id: str
    node: str
    #: Provider-specific guest type. Proxmox: "lxc" or "qemu".
    kind: str
    name: str
    addresses: tuple[str, ...] = ()
    running: bool = True

    def as_payload(self) -> dict:
        """What gets persisted on the link and cached in the snapshot."""
        return {
            "node": self.node,
            "kind": self.kind,
            "name": self.name,
            "addresses": list(self.addresses),
            "running": self.running,
        }


@dataclass(frozen=True)
class Node:
    """One host in a provider's cluster — the row behind §7d's nodes table.

    Every measurement is optional because a node can be online and still not
    answer the per-node read that carries load average and version: a node that
    drops out between the cluster listing and its own status call must produce a
    row with the columns we have, not disappear from the table.

    `storage_*` is that node's **local** storage only. Shared storage is visible
    from every node and belongs to the cluster, so counting it per node would
    print the same 49 TiB NFS share on all four rows and imply four of them.
    """

    name: str
    online: bool = True
    #: Management address, for a link to the node's own web UI.
    #:
    #: **Published on the public panel endpoint by operator ruling of 2026-08-07**,
    #: which extends the 2026-08-06 "cluster data is public" decision from names
    #: and capacity to addresses. Recorded in AS_BUILT §7; a test pins it as
    #: intentionally present so it is not later "fixed" as a leak.
    ip: str = ""
    uptime_s: int | None = None
    #: 0-100, not 0-1 — providers report fractions and UIs want percentages, so
    #: the conversion happens once, here, rather than in every consumer.
    cpu_pct: float | None = None
    #: Logical CPUs, which is what "of 60 CPUs" means. Not physical cores.
    cpu_count: int | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    #: One-minute load average.
    load_avg: float | None = None
    storage_used_bytes: int | None = None
    storage_total_bytes: int | None = None
    version: str = ""

    def as_payload(self) -> dict:
        return {
            "name": self.name,
            "online": self.online,
            "ip": self.ip,
            "uptime_s": self.uptime_s,
            "cpu_pct": self.cpu_pct,
            "cpu_count": self.cpu_count,
            "mem_used_bytes": self.mem_used_bytes,
            "mem_total_bytes": self.mem_total_bytes,
            "load_avg": self.load_avg,
            "storage_used_bytes": self.storage_used_bytes,
            "storage_total_bytes": self.storage_total_bytes,
            "version": self.version,
        }


#: `ha` values. A provider that cannot tell reports UNKNOWN rather than guessing
#: NOT_CONFIGURED, because "HA is off" and "we could not ask" are different
#: answers and only one of them should reassure anybody.
HA_ENABLED = "enabled"
HA_NOT_CONFIGURED = "not-configured"
HA_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Infrastructure:
    """The cluster behind §7d's summary row: health, compute and storage.

    Separate from `Guest` because it answers a different question and can fail
    independently: enrichment needs guests and does not care about load average,
    so a provider that can enumerate guests but not read node status must still
    produce locations. `poll_provider` enforces that ordering.

    **Storage totals are deduplicated across nodes**, which is not a detail. On
    the reference cluster, 7 of 11 storages are shared NFS mounts visible from
    all four nodes, so summing the rows as they arrive reports **1331 TiB
    against a real 336 TiB** — a four-fold overstatement produced entirely by
    successful reads, with nothing anywhere to indicate it is wrong.
    """

    cluster_name: str = ""
    #: None when the provider has no notion of quorum (a single host).
    quorate: bool | None = None
    ha: str = HA_UNKNOWN
    nodes: tuple[Node, ...] = ()
    storages_total: int = 0
    storages_online: int = 0
    storage_used_bytes: int = 0
    storage_total_bytes: int = 0

    def as_payload(self) -> dict:
        return {
            "cluster_name": self.cluster_name,
            "quorate": self.quorate,
            "ha": self.ha,
            "nodes": [node.as_payload() for node in self.nodes],
            "storages_total": self.storages_total,
            "storages_online": self.storages_online,
            "storage_used_bytes": self.storage_used_bytes,
            "storage_total_bytes": self.storage_total_bytes,
        }


@dataclass(frozen=True)
class ProviderStatus:
    """Whether the provider is usable, and if not, precisely why.

    The distinction between `permitted=False` and an empty guest list is the
    whole point — see the module docstring.
    """

    configured: bool
    reachable: bool = False
    permitted: bool = False
    detail: str = ""
    #: Privileges the credential actually holds, when the provider can report
    #: them. Diagnostic only; never rendered to an anonymous caller.
    privileges: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return self.configured and self.reachable and self.permitted

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "reachable": self.reachable,
            "permitted": self.permitted,
            "usable": self.usable,
            "detail": self.detail,
            # Cached so the admin provider row can show what the credential
            # actually holds (audit informational). Privilege names are
            # diagnostic infrastructure data: they ride the snapshot payload,
            # which only the admin-scoped settings row serialises — `panels.py`
            # emits its own closed strings and never forwards this.
            "privileges": list(self.privileges),
        }


@runtime_checkable
class Provider(Protocol):
    """What enrichment is written against. Implementations live beside this."""

    #: Stable key stored in `service_links.provider`. Never a display name — it
    #: is persisted, so renaming it is a data migration.
    name: str

    def is_configured(self) -> bool:
        """Are there credentials? Cheap, no network, safe on a request path."""
        ...

    def status(self) -> ProviderStatus:
        """Configured, reachable and permitted. May raise `ProviderUnavailable`."""
        ...

    def guests(self, status: ProviderStatus | None = None) -> list[Guest]:
        """Every guest with whatever addresses are discoverable.

        Raises `ProviderUnavailable` rather than returning `[]` when the
        provider cannot answer — an empty list means "this provider genuinely
        has no guests" and nothing else.

        `status` is optional: the poll pass hands in the answer it already has
        so one poll performs one status read, and every other caller omits it.
        """
        ...

    def location_for(self, guest: Guest) -> str:
        """The human-readable location string shown in the UI."""
        ...

    def infrastructure(
        self, status: ProviderStatus | None = None
    ) -> Infrastructure | None:
        """The cluster behind §7d's summary row, or `None`.

        `None` is the honest answer for a provider with no notion of a cluster —
        a Docker or TrueNAS provider enumerates guests and has no nodes table to
        draw. It is **not** the answer for a provider that has one and could not
        read it: that raises `ProviderUnavailable`, so the cache can record why
        instead of rendering an empty panel that looks like a configured cluster
        with nothing in it.
        """
        ...
