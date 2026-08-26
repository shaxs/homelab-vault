"""A fake TrueNAS SCALE box, presented as a WebSocket JSON-RPC session.

Stubbed at the **`_connect` seam** (TN-WS): the provider's `_connect()` is
monkeypatched to hand back this box, which answers `call(method, params)` the
way a live `_Session` would — so `_login`, `status()`, `report()`, the topology
walk and every best-effort degradation run for real, and only the socket is
fake. This replaces the REST-era `httpx.MockTransport` stub; the fixtures
(pools, disks, alerts) are unchanged, because the WS `*.query` methods answer
with the same rows the REST bridge used to serve — verified live, 2026-08-12.

The default box is arranged to exercise every branch the operator cares about:
a healthy pool with capacity on the pool object, a **DEGRADED** pool whose
capacity comes from the root-dataset fallback and which has a scrub running, a
disk whose SMART test **FAILED**, disk serials present in the fixture (so a test
can prove the provider drops them), and one active **CRITICAL** alert beside a
dismissed one (so a test can prove dismissed alerts are filtered).
"""

from __future__ import annotations

from services.providers.base import ProviderUnavailable

GIB = 1024 ** 3
TIB = 1024 ** 4


DEFAULT_SYSTEM = {
    "version": "TrueNAS-SCALE-24.10.1",
    "hostname": "truenas",
    "uptime_seconds": 864000,
    "physmem": 64 * GIB,
    "loadavg": [0.52, 0.40, 0.31],
    "cores": 12,
}


def _topology(members):
    """A minimal SCALE `topology`: one RAIDZ data vdev over `members` devnames.

    This is where the disk↔pool link lives — verified against 25.04,
    `disk.query` leaves each row's own `pool` field null, and the truth is in
    the pool's topology tree. The fake mirrors that so the provider is exercised
    the way the real box actually drives it (the disk-pool-mapping finding,
    2026-08-12).
    """
    return {
        "data": [{"type": "RAIDZ2", "children":
                  [{"type": "DISK", "disk": d} for d in members]}],
        "cache": [], "log": [], "spare": [], "special": [], "dedup": [],
    }


def pool(name, status="ONLINE", healthy=True, size=None, allocated=None,
         free=None, scan=None, capacity_on_object=True, members=()):
    """One `pool.query` row. `capacity_on_object=False` omits size/allocated/free
    so the provider must fall back to the root dataset — the older-SCALE path.
    `members` are the devnames carried in this pool's `topology`."""
    row = {
        "name": name, "status": status, "healthy": healthy, "scan": scan,
        "topology": _topology(members),
    }
    if capacity_on_object:
        row["size"] = size
        row["allocated"] = allocated
        row["free"] = free
    return row


SCRUB_RUNNING = {"function": "SCRUB", "state": "SCANNING", "percentage": 42.5}

DEFAULT_POOLS = [
    pool("tank", size=10 * TIB, allocated=6 * TIB, free=4 * TIB,
         members=["sda", "sdb"]),
    # DEGRADED, capacity via dataset fallback, mid-scrub — the interesting row.
    pool("backup", status="DEGRADED", healthy=False, scan=SCRUB_RUNNING,
         capacity_on_object=False, members=["sdc"]),
]

#: Root-dataset capacity for pools whose `pool.query` row omits it (the
#: fallback, served via `pool.dataset.query` filtered by id).
DEFAULT_DATASETS = {
    "backup": {"used": {"parsed": 3 * TIB}, "available": {"parsed": 1 * TIB}},
}


def disk(name, model, dtype="HDD", size=4 * TIB, serial="SN-SECRET"):
    return {
        "name": name,
        "model": model,
        "type": dtype,
        "size": size,
        # TrueNAS 25.04 leaves this null; membership is derived from pool
        # topology (see `_topology`). Kept here to mirror the real row shape.
        "pool": None,
        # Present in the fixture on purpose: the provider must NOT carry it.
        "serial": serial,
    }


DEFAULT_DISKS = [
    disk("sda", "WD40EFRX", serial="WD-AAAA"),
    disk("sdb", "WD40EFRX", serial="WD-BBBB"),
    disk("sdc", "ST4000VN008", serial="ST-CCCC"),
]

DEFAULT_TEMPERATURES = {"sda": 34, "sdb": 36, "sdc": 41}

#: `smart.test.results` — sdc's most recent test FAILED (the failing drive).
DEFAULT_SMART = [
    {"disk": "sda", "tests": [{"status": "SUCCESS"}]},
    {"disk": "sdb", "tests": [{"status": "SUCCESS"}]},
    {"disk": "sdc", "tests": [{"status": "SUCCESS"}, {"status": "FAILED"}]},
]

DEFAULT_REPLICATION = [
    {"name": "tank-to-backup", "state": {"state": "FINISHED",
                                         "datetime": {"$date": 1_700_000_000_000}}},
]

DEFAULT_CLOUDSYNC = [
    {"description": "backblaze-offsite", "job": {"state": "FAILED",
                                                 "time_finished": {"$date": 1_700_000_500_000}}},
]

#: One active CRITICAL (the failing disk) and one dismissed alert that must be
#: filtered out.
DEFAULT_ALERTS = [
    {"uuid": "alert-1", "level": "CRITICAL", "dismissed": False,
     "klass": "ZpoolCapacityNotice",
     "formatted": "Pool backup is DEGRADED: disk sdc has failed.",
     "datetime": {"$date": 1_700_000_900_000}},
    {"uuid": "alert-2", "level": "WARNING", "dismissed": True,
     "klass": "Old", "formatted": "Dismissed, must not appear.",
     "datetime": {"$date": 1_699_000_000_000}},
]


class FakeTrueNAS:
    """Answers the JSON-RPC methods the provider actually calls.

    Doubles as the session object `_connect` hands back — `call()` routes by
    method name and `close()` records itself, so a test can assert the session
    is not leaked. A failure mode raises `ProviderUnavailable` exactly as the
    real `_Session.call` does for a JSON-RPC error, which is what keeps the
    provider's best-effort branches honest.
    """

    def __init__(self, system=None, pools=None, datasets=None, disks=None,
                 temperatures=None, smart=None, replication=None, cloudsync=None,
                 alerts=None, temperatures_fail=False, smart_fail=False,
                 login_response="SUCCESS"):
        self.system = DEFAULT_SYSTEM if system is None else system
        self.pools = list(DEFAULT_POOLS if pools is None else pools)
        self.datasets = dict(DEFAULT_DATASETS if datasets is None else datasets)
        self.disks = list(DEFAULT_DISKS if disks is None else disks)
        self.temperatures = (
            DEFAULT_TEMPERATURES if temperatures is None else temperatures
        )
        self.smart = list(DEFAULT_SMART if smart is None else smart)
        self.replication = list(
            DEFAULT_REPLICATION if replication is None else replication
        )
        self.cloudsync = list(DEFAULT_CLOUDSYNC if cloudsync is None else cloudsync)
        self.alerts = list(DEFAULT_ALERTS if alerts is None else alerts)
        #: When True, `disk.temperatures` errors — the spun-up-disk timeout
        #: case, which must degrade to no temperatures rather than fail report().
        self.temperatures_fail = temperatures_fail
        #: When True, `smart.test.results` errors — the live 25.04.2.6 case,
        #: where the middleware's own response schema rejects its output.
        self.smart_fail = smart_fail
        #: What `auth.login_ex` answers — "SUCCESS", or "EXPIRED"/"AUTH_ERR"/…
        #: to exercise the closed failure strings.
        self.login_response = login_response
        self.calls: list[str] = []
        self.closed = 0

    # --- the `_Session` surface -------------------------------------------

    def call(self, method: str, params=None):
        self.calls.append(method)
        if method == "auth.login_ex":
            return {"response_type": self.login_response}
        if method == "system.info":
            return dict(self.system)
        if method == "pool.query":
            return list(self.pools)
        if method == "pool.dataset.query":
            # params = [[["id", "=", name]]] — the capacity-fallback filter.
            name = params[0][0][2] if params and params[0] else ""
            found = self.datasets.get(name)
            return [found] if found else []
        if method == "disk.query":
            return list(self.disks)
        if method == "disk.temperatures":
            if self.temperatures_fail:
                raise ProviderUnavailable(
                    "TrueNAS returned a JSON-RPC error -32001 for disk.temperatures (EFAULT)"
                )
            return dict(self.temperatures)
        if method == "smart.test.results":
            if self.smart_fail:
                raise ProviderUnavailable(
                    "TrueNAS returned a JSON-RPC error -32001 for smart.test.results (EINVAL)"
                )
            return list(self.smart)
        if method == "replication.query":
            return list(self.replication)
        if method == "cloudsync.query":
            return list(self.cloudsync)
        if method == "alert.list":
            return list(self.alerts)
        raise ProviderUnavailable(
            f"TrueNAS returned a JSON-RPC error -32601 for {method} (ENOMETHOD)"
        )

    def close(self) -> None:
        self.closed += 1


def install(monkeypatch, provider, box: "FakeTrueNAS"):
    """Point `provider._connect()` at `box` without changing its construction."""

    def _connect(self=None):
        return box

    monkeypatch.setattr(type(provider), "_connect", _connect)
    return box


def install_unreachable(monkeypatch, provider, reason="ConnectionRefusedError: refused"):
    """Make the socket itself fail, the way an unreachable box would — the
    real `_connect` wraps socket errors in `ProviderUnavailable` too."""

    def _connect(self=None):
        raise ProviderUnavailable(reason)

    monkeypatch.setattr(type(provider), "_connect", _connect)


def install_failure(monkeypatch, provider, exc=None, status_code=None):
    """Compat shim for the REST-era call sites.

    `exc` (a connection error) → the socket fails; `status_code=401` → the box
    answers but refuses the login, which is the WS meaning of the old 401 path.
    """
    if exc is not None:
        install_unreachable(monkeypatch, provider, f"{type(exc).__name__}: {exc}")
    else:
        response = "AUTH_ERR" if status_code == 401 else "EXPIRED"
        install(monkeypatch, provider, FakeTrueNAS(login_response=response))
