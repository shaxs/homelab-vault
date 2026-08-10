"""Proxmox VE, read-only — v1 §7a, §7b.

**Everything non-obvious in this module came from running the §7b buildability
check against the real cluster on 2026-08-06, and three of the findings
contradict what the spec assumed.** They are written here rather than only in
BUILD_LOG because each one is a trap that reads as working code.

**1. An LXC's address is NOT in its config.** §7b says "LXC config carries them
directly", and that is true only for statically-addressed containers. All 36
containers in the reference lab are `ip=dhcp`, and a DHCP container's `net0`
holds no address at all — the config is a declaration of intent, and only the
*running* container knows its lease. So the primary source is
`/nodes/{node}/lxc/{vmid}/interfaces`, with a static `ip=` in `net0` as the
fallback for the rarer pinned case. Built the other way round, enrichment
matched 1 of 19 catalogued addresses; built this way, 14 of 19.

**2. That failure is silent.** All 36 config reads *succeeded* — there was no
error to notice, the address simply was not in the response. Which is the same
shape as finding 3.

**3. A permissions failure looks like an empty cluster.** Proxmox filters
collections by privilege and answers `200 []` rather than 403, so a token with
no ACL enumerates zero guests through a completely healthy-looking call. That is
why `status()` asks `/access/permissions` directly: an empty mapping is
conclusive, and it is the only way to tell "no guests" from "may not see the
guests". See `base.ProviderStatus`.

**4. `VM.Monitor` is not required.** PVE 8+ split read-only guest-agent queries
out of `VM.Monitor` into `VM.GuestAgent.Audit`, which `PVEAuditor` carries —
confirmed against 9.1.1, where the token reports `VM.Monitor=False` and
`network-get-interfaces` succeeds anyway. This matters beyond convenience: §7a's
"read-only by construction" is the entire argument for not building secret
storage and revocation, and needing `VM.Monitor` would have weakened it.

**On SSRF (§4c, owasp A10).** `PROXMOX_HOST` is deploy-time configuration read
from the environment, never from a request, so it is not a user-controlled
outbound path and the probe's address validator does not apply to it — it sits
at the same trust level as `DATABASE_URL`. What *would* be user-controlled is a
provider host configurable through the settings table, which is precisely why
§6a keeps credentials and endpoints out of that table.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from .base import (
    HA_ENABLED,
    HA_NOT_CONFIGURED,
    HA_UNKNOWN,
    Guest,
    Infrastructure,
    Node,
    ProviderStatus,
    ProviderUnavailable,
)

logger = logging.getLogger("services.providers.proxmox")

#: Matches a static address in an LXC `netN` line: `ip=192.168.0.108/24`.
#: `ip=dhcp` deliberately does not match — see finding 1.
_STATIC_IP_RE = re.compile(r"\bip=(\d{1,3}(?:\.\d{1,3}){3})/")

#: Guest kinds, mapped to the abbreviation the location string uses. Matches the
#: `pve-c · CT 117` format already used for `host_label` elsewhere.
_KIND_LABEL = {"lxc": "CT", "qemu": "VM"}


class ProxmoxProvider:
    """The first implementation of the §7a seam."""

    name = "proxmox"

    # --- Configuration -----------------------------------------------------

    def is_configured(self) -> bool:
        """Credentials present. No network — this is called on request paths."""
        return bool(self._host() and self._token())

    @staticmethod
    def _host() -> str:
        return (getattr(settings, "PROXMOX_HOST", "") or "").strip().rstrip("/")

    @staticmethod
    def _token() -> str:
        return (getattr(settings, "PROXMOX_API_TOKEN", "") or "").strip()

    def _base_url(self) -> str:
        """Validate `PROXMOX_HOST` and name the part that is actually wrong.

        Raised rather than defaulted to `https://` so a misconfiguration is named
        once, loudly, instead of producing a confusing 401 on every poll — httpx
        would otherwise raise `UnsupportedProtocol` per call.

        **The scheme and the host are checked separately**, because the first
        version tested only `startswith("https://")` and so told an operator who
        had typed exactly `https://` that it "must include the scheme" — pointing
        at the one part of the line that was right. An error that names the wrong
        half is worse than a generic one: it sends you to re-check what you just
        did correctly.
        """
        host = self._host()
        example = "e.g. https://192.168.0.101:8006 — the PVE API is HTTPS on 8006."
        parts = urlsplit(host)
        if parts.scheme not in ("http", "https"):
            raise ProviderUnavailable(f"PROXMOX_HOST must start with https://, {example}")
        if not parts.netloc:
            raise ProviderUnavailable(
                f"PROXMOX_HOST has a scheme but no address — it is just "
                f"{host!r}. Add the node's host and port, {example}"
            )
        return f"{host}/api2/json"

    def _client(self) -> httpx.Client:
        _warn_once_about_unverified_tls()
        return httpx.Client(
            base_url=self._base_url(),
            headers={"Authorization": self._token()},
            # Explicit and named, never a hidden verify=False (V0 finding #4).
            # Proxmox ships a self-signed certificate on 8006 by default.
            verify=settings.PROXMOX_VERIFY_TLS,
            timeout=httpx.Timeout(settings.PROXMOX_TIMEOUT_S),
            # **Never follow a redirect on this client.** httpx would replay the
            # request — including the `Authorization` header — at whatever
            # location the response names. The probe can afford redirects
            # because it carries no credentials; this client carries one on
            # every request, so the only safe number of hops is zero.
            follow_redirects=False,
        )

    # --- Reading -----------------------------------------------------------

    def _get(self, client: httpx.Client, path: str, expect: type | None = None):
        """One GET. Returns `data`, or raises `ProviderUnavailable`.

        A 403 is raised rather than swallowed: it means the credential is real
        but under-privileged, which is a configuration problem the operator has
        to see. Callers that expect a 403 on a specific path handle it.

        **The envelope and the `data` type are validated, never assumed**
        (audit S6). A 200 whose body is not the `{"data": ...}` mapping is not
        the PVE API answering — it is a reverse proxy's error page or a changed
        endpoint — and before this check it surfaced as an `AttributeError`
        that bypassed failed-poll recording entirely. The body is deliberately
        never quoted in the error: an error page can carry anything.
        """
        try:
            response = client.get(path)
        except httpx.HTTPError as exc:
            # UnsupportedProtocol (missing scheme) IS an HTTPError, unlike
            # httpx.InvalidURL — verified, not assumed. Both land here.
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 401:
            raise ProviderUnavailable(
                "Proxmox refused the credential (401). PROXMOX_API_TOKEN must be "
                "the complete PVEAPIToken=USER@REALM!TOKENID=UUID string, not "
                "only the UUID."
            )
        if response.status_code != 200:
            raise ProviderUnavailable(
                f"Proxmox returned HTTP {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(f"Proxmox returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(
                f"Proxmox returned an unexpected response shape for {path}"
            )
        data = payload.get("data")
        if data is None or expect is None:
            return data
        if not isinstance(data, expect):
            raise ProviderUnavailable(
                f"Proxmox returned an unexpected data shape for {path}"
            )
        if expect is list and not all(isinstance(row, dict) for row in data):
            # Every PVE collection is a list of objects; anything else is not
            # this API, and `.get()` on it would crash past the poll recorder.
            raise ProviderUnavailable(
                f"Proxmox returned an unexpected data shape for {path}"
            )
        return data

    def status(self) -> ProviderStatus:
        """Configured, reachable, and — critically — permitted.

        `/access/permissions` returns the *effective* privileges of whoever is
        asking. An empty mapping is proof the token is blind, and it is the only
        cheap signal that distinguishes that from a cluster with no guests.
        """
        if not self.is_configured():
            return ProviderStatus(configured=False, detail="No Proxmox credentials set.")
        try:
            with self._client() as client:
                perms = self._get(client, "/access/permissions", expect=dict) or {}
        except ProviderUnavailable as exc:
            return ProviderStatus(configured=True, reachable=False, detail=str(exc))

        privileges = tuple(sorted({
            name
            for scope in perms.values() if isinstance(scope, dict)
            for name, on in scope.items() if on
        }))
        if not privileges:
            return ProviderStatus(
                configured=True,
                reachable=True,
                permitted=False,
                detail=(
                    "The API token authenticates but holds no privileges, so it "
                    "cannot see any guests. Grant it the PVEAuditor role — note "
                    "that with privilege separation the token's rights are the "
                    "intersection of the user's and the token's, so BOTH need "
                    "the grant:\n"
                    "  pveum acl modify / --users 'USER@REALM' --roles PVEAuditor\n"
                    "  pveum acl modify / --tokens 'USER@REALM!TOKENID' --roles PVEAuditor"
                ),
                privileges=(),
            )
        return ProviderStatus(
            configured=True, reachable=True, permitted=True, privileges=privileges
        )

    def guests(self, status: ProviderStatus | None = None) -> list[Guest]:
        """Every guest in the cluster, with whatever addresses are discoverable.

        Raises `ProviderUnavailable` when the provider cannot answer — including
        when it is blind, because returning `[]` there is the silent failure this
        whole module is arranged to prevent.

        `status` lets a caller that has already asked (the poll pass) hand the
        answer in, so one poll costs one `/access/permissions` round trip
        instead of three. Left out, it is asked for — every existing caller
        keeps working.
        """
        status = status or self.status()
        if not status.configured:
            raise ProviderUnavailable("No Proxmox credentials set.")
        if not status.reachable or not status.permitted:
            raise ProviderUnavailable(status.detail)

        with self._client() as client:
            rows = self._get(client, "/cluster/resources?type=vm", expect=list) or []
            return [
                guest
                for guest in (self._one_guest(client, row) for row in rows)
                if guest is not None
            ]

    def _one_guest(self, client: httpx.Client, row: dict) -> Guest | None:
        kind = row.get("type")
        vmid, node = row.get("vmid"), row.get("node")
        if kind not in _KIND_LABEL or vmid is None or not node:
            return None
        running = row.get("status") == "running"
        addresses: tuple[str, ...] = ()
        if running:
            # A stopped guest has no lease to report. It keeps its link (see
            # `last_seen_at`) — a guest being down is not a guest being gone.
            try:
                addresses = (
                    self._lxc_addresses(client, node, vmid)
                    if kind == "lxc"
                    else self._qemu_addresses(client, node, vmid)
                )
            except ProviderUnavailable as exc:
                # One unreadable guest must not lose the other 36. The guest is
                # still returned so its link survives and its node stays current;
                # only the address is missing.
                logger.warning("could not read addresses for %s %s: %s", kind, vmid, exc)
        return Guest(
            foreign_id=str(vmid),
            node=node,
            kind=kind,
            name=row.get("name") or str(vmid),
            addresses=addresses,
            running=running,
        )

    def _lxc_addresses(self, client, node: str, vmid) -> tuple[str, ...]:
        """Running interfaces first, static config second — see finding 1."""
        found: list[str] = []
        interfaces = self._get(
            client, f"/nodes/{node}/lxc/{vmid}/interfaces", expect=list
        )
        for iface in interfaces or []:
            inet = iface.get("inet") or ""
            if inet:
                found.append(inet.split("/")[0])
        if not found:
            config = (
                self._get(client, f"/nodes/{node}/lxc/{vmid}/config", expect=dict)
                or {}
            )
            for key, value in config.items():
                if key.startswith("net") and isinstance(value, str):
                    found.extend(_STATIC_IP_RE.findall(value))
        return _usable(found)

    def _qemu_addresses(self, client, node: str, vmid) -> tuple[str, ...]:
        """Via the guest agent, which needs only `VM.GuestAgent.Audit`.

        A VM without the agent installed or running answers 500, which arrives
        here as `ProviderUnavailable` and is logged per guest rather than
        failing the pass — that is §7b's "expect partial coverage", and it is
        the *correct* reason for it.
        """
        data = self._get(
            client,
            f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
            expect=dict,
        )
        found: list[str] = []
        result = (data or {}).get("result")
        for iface in result if isinstance(result, list) else []:
            if not isinstance(iface, dict):
                continue
            entries = iface.get("ip-addresses")
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("ip-address-type") == "ipv4":
                    found.append(entry.get("ip-address") or "")
        return _usable(found)

    # --- Infrastructure (§7d) ----------------------------------------------

    def infrastructure(
        self, status: ProviderStatus | None = None
    ) -> Infrastructure:
        """The cluster, its nodes and its storage — §7d's summary row and table.

        Four reads plus one per online node. Every shape here was taken from the
        live cluster on 2026-08-06 rather than from the spec, which names
        `/cluster/status` and `/nodes` without saying what is in them — and two
        of the four would have been built wrong from the names alone:

        **`/cluster/status`'s cluster entry has a `version` field and it is not
        the PVE version.** It is the corosync configuration version — `8` on a
        cluster running 9.1.1. Rendering it in §7d's Version column would have
        produced a plausible number that is wrong by a whole major release, on
        every row, with every read succeeding. The version comes from
        `/nodes/{node}/status`'s `pveversion` and is per node, which is also the
        more useful answer: a half-upgraded cluster is exactly when you look.

        **`/nodes` carries `ssl_fingerprint`.** V1.5's panel endpoint is public
        by operator ruling, so a field-forwarding implementation would publish
        every node's certificate fingerprint. `Node` has no such field, which
        makes the omission structural rather than a line somebody has to
        remember not to add.

        `status` is the same pre-computed-answer seam `guests()` has.
        """
        status = status or self.status()
        if not status.configured:
            raise ProviderUnavailable("No Proxmox credentials set.")
        if not status.reachable or not status.permitted:
            raise ProviderUnavailable(status.detail)

        with self._client() as client:
            cluster_name, quorate, addresses = self._cluster_identity(client)
            # Storage first: its rows carry each node's local totals, so the
            # nodes table is built with them rather than patched afterwards.
            storage = self._storage(client)
            return Infrastructure(
                cluster_name=cluster_name,
                quorate=quorate,
                ha=self._ha(client),
                nodes=self._nodes(client, storage["by_node"], addresses),
                storages_total=storage["total_count"],
                storages_online=storage["online_count"],
                storage_used_bytes=storage["used_bytes"],
                storage_total_bytes=storage["total_bytes"],
            )

    def _cluster_identity(
        self, client: httpx.Client
    ) -> tuple[str, bool | None, dict[str, str]]:
        """Name, quorum and node addresses from `/cluster/status`.

        A standalone (unclustered) PVE host has **no** `type=cluster` entry at
        all, only its own node entry, so the name stays empty rather than being
        invented. Quorum is `None` there, not `False`: one host cannot lose a
        vote it never had, and reporting "not quorate" would be a red panel on a
        healthy lab.

        **The addresses come from here because `/nodes` does not carry them.**
        Note what they are: the address each node is reachable at *on the cluster
        ring*. On the ordinary homelab that is also the management address, which
        is what makes a link to `https://<ip>:8006` work — but a cluster with a
        separate corosync network would report the ring address, and the link
        would point at the wrong interface. Worth knowing before treating this as
        a general-purpose management address.
        """
        name, quorate = "", None
        addresses: dict[str, str] = {}
        for row in self._get(client, "/cluster/status", expect=list) or []:
            if row.get("type") == "cluster":
                name, quorate = row.get("name") or "", bool(row.get("quorate"))
            elif row.get("type") == "node" and row.get("name"):
                addresses[row["name"]] = row.get("ip") or ""
        return name, quorate, addresses

    def _nodes(
        self, client: httpx.Client, local_storage: dict, addresses: dict
    ) -> tuple[Node, ...]:
        """`/nodes` for the cluster-wide numbers, then one call per online node.

        The per-node call is what carries load average and version, and it is
        made **best-effort per node** for the same reason `_one_guest` is: a node
        that goes offline mid-poll must cost its own two columns, not the other
        three rows.
        """
        rows = self._get(client, "/nodes", expect=list) or []
        nodes = []
        for row in sorted(rows, key=lambda r: r.get("node") or ""):
            name = row.get("node")
            if not name:
                continue
            online = row.get("status") == "online"
            cpu_count = row.get("maxcpu")
            load_avg, version = None, ""
            if online:
                load_avg, version = self._node_detail(client, name)
            used, total = local_storage.get(name, (None, None))
            nodes.append(
                Node(
                    name=name,
                    online=online,
                    ip=addresses.get(name, ""),
                    uptime_s=row.get("uptime"),
                    # PVE reports a 0-1 fraction; `Node.cpu_pct` is a percentage.
                    cpu_pct=_pct(row.get("cpu")),
                    cpu_count=cpu_count,
                    mem_used_bytes=row.get("mem"),
                    mem_total_bytes=row.get("maxmem"),
                    load_avg=load_avg,
                    storage_used_bytes=used,
                    storage_total_bytes=total,
                    version=version,
                )
            )
        return tuple(nodes)

    def _node_detail(self, client: httpx.Client, node: str) -> tuple[float | None, str]:
        """Load average and PVE version for one node, or `(None, "")`."""
        try:
            data = self._get(client, f"/nodes/{node}/status", expect=dict) or {}
        except ProviderUnavailable as exc:
            logger.warning("could not read status for node %s: %s", node, exc)
            return None, ""
        load = (data.get("loadavg") or [None])[0]
        try:
            load_avg = float(load)
        except (TypeError, ValueError):
            load_avg = None
        return load_avg, _pve_version(data.get("pveversion"))

    def _storage(self, client: httpx.Client) -> dict:
        """Storage totals, **deduplicated**, plus each node's local storage.

        `/cluster/resources?type=storage` returns one row per storage *per node
        that can see it*, so a shared NFS mount appears once for every node. On
        the reference cluster that is 40 rows for 16 distinct storages, and
        summing them as they arrive reports **1331 TiB against a real 336 TiB**.
        Nothing about it looks wrong: every read succeeds and every row is
        genuine.

        The dedupe key is the storage *name* when it is shared and
        `(node, name)` when it is not — because `local` and `local-lvm` exist on
        every node and are four different disks, while `shared-nas` is one
        NAS seen four times.

        **A limit that remains, and is stated in the UI rather than hidden:**
        several NFS storages can be different exports of one pool. They have
        distinct names, report that pool's size each, and no API field
        distinguishes them, so cluster capacity still over-counts a
        multi-export NAS. Proxmox's own datacenter summary has the same
        property.
        """
        rows = self._get(
            client, "/cluster/resources?type=storage", expect=list
        ) or []
        seen: set = set()
        total_bytes = used_bytes = online_count = 0
        by_node: dict[str, list[int]] = {}
        for row in rows:
            name, node = row.get("storage"), row.get("node")
            if not name:
                continue
            shared = bool(row.get("shared"))
            key = name if shared else (node, name)
            if key in seen:
                continue
            seen.add(key)
            total_bytes += row.get("maxdisk") or 0
            used_bytes += row.get("disk") or 0
            if row.get("status") == "available":
                online_count += 1
            if not shared and node:
                # Only local storage is attributed to a node — see `Node`.
                tally = by_node.setdefault(node, [0, 0])
                tally[0] += row.get("disk") or 0
                tally[1] += row.get("maxdisk") or 0
        return {
            "total_count": len(seen),
            "online_count": online_count,
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "by_node": by_node,
        }

    def _ha(self, client: httpx.Client) -> str:
        """Whether HA is actually configured, from `/cluster/ha/status/current`.

        The endpoint answers 200 with a single `type=quorum` entry on a cluster
        with **no** HA configured, so "the call succeeded" and "HA is on" are
        different facts. HA is reported as enabled only when a managed service
        or a node manager is present.

        A failure here reports `unknown`, never `not-configured`: this read needs
        `Sys.Audit`, and a token that lacks it must not make an unmonitored
        cluster look deliberately unmonitored.
        """
        try:
            rows = self._get(
                client, "/cluster/ha/status/current", expect=list
            ) or []
        except ProviderUnavailable as exc:
            logger.warning("could not read HA status: %s", exc)
            return HA_UNKNOWN
        kinds = {row.get("type") for row in rows}
        return HA_ENABLED if kinds - {"quorum"} else HA_NOT_CONFIGURED

    # --- Presentation ------------------------------------------------------

    def location_for(self, guest: Guest) -> str:
        """`pve-c · CT 105` — node first, because that is what migration changes."""
        return f"{guest.node} · {_KIND_LABEL.get(guest.kind, guest.kind.upper())} {guest.foreign_id}"


#: Process-local latch so the warning below is said once, not once per request.
_tls_warning_said = False


def _warn_once_about_unverified_tls() -> None:
    """Say plainly that a credential is crossing an unverified TLS connection.

    **`PROXMOX_VERIFY_TLS=False` is NOT the same trade-off as
    `PROBE_VERIFY_TLS=False`, even though the two settings look symmetric — and
    that symmetry is exactly what makes this easy to wave through.** The probe
    sends **no credentials**; the worst a man in the middle gets from it is a
    forged page title. This client sends the `PVEAPIToken` on **every** request,
    so with verification off a man in the middle on the LAN can simply take it.

    Left as the default anyway, because Proxmox ships a self-signed certificate
    on 8006 and defaulting to True would make the feature fail closed for
    essentially every homelab — which is how a security setting ends up disabled
    globally instead of considered. What it must not be is *silent*: this is the
    one place the app knowingly puts a credential on a connection it cannot
    authenticate, so it says so, once per process, at WARNING.

    Bounded by the token being read-only by construction (§7a) and by
    `follow_redirects=False`, so a stolen token grants cluster *reads* and the
    header is never replayed to a third-party host.
    """
    global _tls_warning_said
    if _tls_warning_said or settings.PROXMOX_VERIFY_TLS:
        return
    _tls_warning_said = True
    logger.warning(
        "PROXMOX_VERIFY_TLS is off: the Proxmox API token is sent over a TLS "
        "connection whose certificate is not verified, so anyone able to "
        "intercept traffic to the cluster can capture it. The token is "
        "read-only (PVEAuditor), which bounds the damage. Set "
        "PROXMOX_VERIFY_TLS=True once the cluster presents a certificate your "
        "trust store accepts."
    )


def _pct(fraction) -> float | None:
    """A 0-1 CPU fraction as a 0-100 percentage, rounded to one decimal."""
    try:
        return round(float(fraction) * 100, 1)
    except (TypeError, ValueError):
        return None


def _pve_version(raw) -> str:
    """`pve-manager/9.1.1/42db4a6cf33dac83` -> `9.1.1`.

    The build hash is dropped deliberately: §7d's column is one of eight in a
    dense table, and on a **public** endpoint the hash narrows a host to an
    exact build with nothing gained by an operator who can read the release.
    An unrecognised shape is returned verbatim rather than blanked — a version
    we cannot parse is still more useful than no version.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parts = text.split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else text


def _usable(candidates) -> tuple[str, ...]:
    """Routable IPv4 only, de-duplicated, order preserved.

    Loopback and link-local are dropped because every guest has them and they
    would match every service against whichever guest was enumerated first —
    turning enrichment into a random-guest generator rather than a no-op, which
    is far worse than finding nothing.
    """
    out: list[str] = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address((candidate or "").strip())
        except ValueError:
            continue
        if address.version != 4 or address.is_loopback or address.is_link_local:
            continue
        if str(address) not in out:
            out.append(str(address))
    return tuple(out)
