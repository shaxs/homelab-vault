"""A fake Proxmox cluster, served through `httpx.MockTransport`.

Stubbing at the *transport* rather than at `ProxmoxProvider`'s methods is
deliberate: it exercises `_get`, `status()`, `guests()`, `_lxc_addresses()` and
`_qemu_addresses()` for real, including the status-code handling and the
DHCP-vs-static fallback, which are exactly the parts the live cluster proved
were easy to get wrong. Stubbing the methods would test the fixture instead.

The default cluster mirrors the shapes actually observed on 2026-08-06:
DHCP-only LXC configs, Docker bridge gateways shared across containers, a
stopped container, and one QEMU VM answering through the guest agent.
"""

from __future__ import annotations

import json

import httpx

GIB = 1024 ** 3
TIB = 1024 ** 4

#: What `PVEAuditor` actually reports on PVE 9.1 — note `VM.Monitor` absent and
#: `VM.GuestAgent.Audit` present, which is why VM addresses are readable.
PVEAUDITOR_PRIVILEGES = {
    "Datastore.Audit": 1,
    "Mapping.Audit": 1,
    "Pool.Audit": 1,
    "SDN.Audit": 1,
    "Sys.Audit": 1,
    "VM.Audit": 1,
    "VM.GuestAgent.Audit": 1,
}


def guest(vmid, node, name, kind="lxc", addresses=("192.168.0.10",), running=True):
    return {
        "vmid": vmid,
        "node": node,
        "name": name,
        "kind": kind,
        "addresses": list(addresses),
        "running": running,
    }


def node(name, online=True, cpu=0.05, maxcpu=16, mem=8 * GIB, maxmem=32 * GIB,
         uptime=600000, version="9.1.1", loadavg="0.42", ip=""):
    return {
        "node": name,
        # Deterministic, from the documentation range (RFC 5737). Never
        # `hash()` — Python randomises string hashes per process, so the
        # fixture would differ between runs.
        "ip": ip or f"192.0.2.{(sum(name.encode()) % 200) + 10}",
        "status": "online" if online else "offline",
        "cpu": cpu,
        "maxcpu": maxcpu,
        "mem": mem,
        "maxmem": maxmem,
        "uptime": uptime,
        "version": version,
        "loadavg": loadavg,
    }


def storage(name, node_name, total, used, shared=False, status="available"):
    return {
        "storage": name,
        "node": node_name,
        "maxdisk": total,
        "disk": used,
        "shared": 1 if shared else 0,
        "status": status,
        "type": "storage",
    }


DEFAULT_NODES = [
    node("pve-d", maxcpu=16, mem=10 * GIB, maxmem=64 * GIB, uptime=609946),
    node("pve-c", maxcpu=20, mem=9 * GIB, maxmem=16 * GIB, uptime=1189913,
         loadavg="0.44"),
    # Deliberately behind: the real cluster was mid-upgrade when this was
    # written, which is exactly why the Version column is per node.
    node("pve-b", maxcpu=20, mem=14 * GIB, maxmem=32 * GIB, cpu=0.118,
         version="9.1.5", loadavg="2.09"),
    node("pve-a", maxcpu=4, mem=4 * GIB, maxmem=8 * GIB, version="9.1.5",
         loadavg="0.00"),
]

#: One shared NFS mount visible from every node plus one local disk each — the
#: shape that makes naive summing report four times the real capacity.
DEFAULT_STORAGES = [
    storage("shared-nas", name, 40 * TIB, 4 * TIB, shared=True)
    for name in ("pve-d", "pve-c", "pve-b", "pve-a")
] + [
    storage("local", "pve-d", 1 * TIB, 100 * GIB),
    storage("local", "pve-c", 2 * TIB, 200 * GIB),
    storage("local", "pve-b", 1 * TIB, 300 * GIB),
    storage("local", "pve-a", 1 * TIB, 50 * GIB),
]

#: What `/cluster/ha/status/current` answers on a cluster with **no** HA
#: configured: 200, one quorum entry, and nothing else. Observed 2026-08-06.
HA_OFF = [{"id": "quorum", "node": "pve-d", "status": "OK", "quorate": 1, "type": "quorum"}]
HA_ON = HA_OFF + [
    {"id": "vm:105", "node": "pve-c", "status": "started", "type": "service"}
]


DEFAULT_GUESTS = [
    # Two Docker hosts sharing 172.17.0.1 — the ambiguity case, from the real lab.
    guest(105, "pve-c", "media-services", addresses=("192.168.0.125", "172.17.0.1")),
    guest(107, "pve-c", "services", addresses=("192.168.0.136", "172.17.0.1")),
    guest(117, "pve-c", "leantime", addresses=("192.168.0.108",)),
    guest(133, "pve-c", "omniroute", addresses=("192.168.0.72",)),
    # Stopped: exists, has no lease to report.
    guest(140, "pve-b", "archived", addresses=(), running=False),
    # The one VM, reachable only through the guest agent.
    guest(124, "pve-a", "home-assistant", kind="qemu", addresses=("192.168.0.88",)),
]


class FakeCluster:
    """Serves the PVE endpoints the provider actually calls."""

    def __init__(
        self,
        guests=None,
        privileges=None,
        static_ip_guests=(),
        nodes=None,
        storages=None,
        ha=None,
        cluster_name="demo-lab",
        unreadable_nodes=(),
    ):
        self.guests = list(DEFAULT_GUESTS if guests is None else guests)
        self.privileges = (
            PVEAUDITOR_PRIVILEGES if privileges is None else privileges
        )
        #: VMIDs whose address must come from `config.net0` instead of
        #: `/interfaces` — the statically-addressed container case.
        self.static_ip_guests = set(static_ip_guests)
        self.nodes = list(DEFAULT_NODES if nodes is None else nodes)
        self.storages = list(DEFAULT_STORAGES if storages is None else storages)
        self.ha = HA_OFF if ha is None else ha
        #: `""` makes `/cluster/status` omit the cluster entry entirely, which
        #: is what a standalone (unclustered) PVE host actually returns.
        self.cluster_name = cluster_name
        #: Nodes whose `/nodes/{name}/status` fails — one node dropping out
        #: mid-poll must not cost the other rows.
        self.unreadable_nodes = set(unreadable_nodes)
        self.requests: list[str] = []

    # --- transport -------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/api2/json", 1)[-1]
        query = request.url.query.decode() if request.url.query else ""
        self.requests.append(path + (f"?{query}" if query else ""))
        body = self._route(path, query)
        if body is None:
            return httpx.Response(500, json={"data": None})
        return httpx.Response(200, json={"data": body})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # --- routes ----------------------------------------------------------

    def _route(self, path: str, query: str = ""):
        if path == "/access/permissions":
            return {"/": dict(self.privileges)} if self.privileges else {}
        if path == "/cluster/status":
            rows = [
                {
                    "id": f"node/{n['node']}",
                    "type": "node",
                    "name": n["node"],
                    # `/cluster/status` is the only endpoint that carries this;
                    # `/nodes` does not.
                    "ip": n["ip"],
                    "online": 1 if n["status"] == "online" else 0,
                }
                for n in self.nodes
            ]
            if self.cluster_name:
                # `version` here is the COROSYNC config version, not the PVE
                # version — the trap that would put "8" in §7d's Version column
                # on a cluster running 9.1.
                rows.insert(
                    0,
                    {
                        "id": "cluster",
                        "type": "cluster",
                        "name": self.cluster_name,
                        "nodes": len(self.nodes),
                        "quorate": 1,
                        "version": 8,
                    },
                )
            return rows
        if path == "/cluster/ha/status/current":
            return list(self.ha)
        if path == "/nodes":
            return [
                {
                    "node": n["node"],
                    "status": n["status"],
                    "cpu": n["cpu"],
                    "maxcpu": n["maxcpu"],
                    "mem": n["mem"],
                    "maxmem": n["maxmem"],
                    "uptime": n["uptime"],
                    # Real `/nodes` carries this. Nothing may forward it: the
                    # panel endpoint is public.
                    "ssl_fingerprint": "AA:BB:CC:DD",
                }
                for n in self.nodes
            ]
        for n in self.nodes:
            if path == f"/nodes/{n['node']}/status":
                if n["node"] in self.unreadable_nodes:
                    return None  # 500, the way a node dropping out behaves
                return {
                    "loadavg": [n["loadavg"], "0.60", "0.60"],
                    "pveversion": f"pve-manager/{n['version']}/42db4a6cf33dac83",
                    "uptime": n["uptime"],
                }
        if path == "/cluster/resources" and "type=storage" in query:
            return list(self.storages)
        if path == "/cluster/resources":
            return [
                {
                    "vmid": g["vmid"],
                    "node": g["node"],
                    "name": g["name"],
                    "type": g["kind"],
                    "status": "running" if g["running"] else "stopped",
                }
                for g in self.guests
            ]
        for g in self.guests:
            base_lxc = f"/nodes/{g['node']}/lxc/{g['vmid']}"
            base_qemu = f"/nodes/{g['node']}/qemu/{g['vmid']}"
            if path == f"{base_lxc}/interfaces":
                if g["vmid"] in self.static_ip_guests:
                    return []  # DHCP-less: nothing running to report
                return [{"name": "eth0", "inet": f"{a}/24"} for a in g["addresses"]] + [
                    {"name": "lo", "inet": "127.0.0.1/8"}
                ]
            if path == f"{base_lxc}/config":
                if g["vmid"] in self.static_ip_guests:
                    ips = ",".join(f"ip={a}/24" for a in g["addresses"])
                    return {"net0": f"name=eth0,bridge=vmbr0,{ips},type=veth"}
                # The real shape: DHCP, therefore no address at all.
                return {"net0": "name=eth0,bridge=vmbr0,ip=dhcp,type=veth"}
            if path == f"{base_qemu}/agent/network-get-interfaces":
                return {
                    "result": [
                        {
                            "name": "eth0",
                            "ip-addresses": [
                                {"ip-address-type": "ipv4", "ip-address": a}
                                for a in g["addresses"]
                            ]
                            + [{"ip-address-type": "ipv6", "ip-address": "fe80::1"}],
                        }
                    ]
                }
        return None


def install(monkeypatch, provider, cluster: FakeCluster):
    """Point `provider._client()` at `cluster` without changing its construction."""

    def _client(self=None):
        return httpx.Client(
            base_url="https://pve.test/api2/json",
            transport=cluster.transport(),
            headers={"Authorization": "PVEAPIToken=t@pve!k=secret"},
        )

    monkeypatch.setattr(type(provider), "_client", _client)
    return cluster


def install_failure(monkeypatch, provider, exc=None, status_code=None):
    """Make every call fail, the way an unreachable or hostile cluster would."""

    def handler(request):
        if exc is not None:
            raise exc
        return httpx.Response(status_code, text=json.dumps({"data": None}))

    def _client(self=None):
        return httpx.Client(
            base_url="https://pve.test/api2/json",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "PVEAPIToken=t@pve!k=secret"},
        )

    monkeypatch.setattr(type(provider), "_client", _client)
