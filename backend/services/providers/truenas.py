"""TrueNAS SCALE, read-only, over WebSocket JSON-RPC — v1 §7a (the second
provider; transport ported from REST in TN-WS).

TrueNAS is a **storage appliance, not a hypervisor**, so it fits the provider
seam differently from Proxmox and that difference is deliberate, not a gap:

- `guests()` is always `[]`. Per `base.py`'s contract an empty list means "this
  provider genuinely has no guests", which is exactly true — there is nothing to
  enrich a catalog service's *location* against, so `location_for` is never
  reached and no `service_links` are written.
- `infrastructure()` is always `None` — the documented "this provider has no
  cluster/nodes table" answer. TrueNAS has pools and disks, not a Proxmox-style
  quorum of nodes, and forcing them through `Node` would misreport both.
- The real work is `report()`, the **optional** capability documented at the
  foot of `base.py`: it returns a `TrueNASReport` the poll pass caches and
  `panels.py` renders as the TrueNAS adapter section. It is not on the `Provider`
  Protocol precisely so a storage provider need not pretend to be a guest one.

**Why WebSocket JSON-RPC and not REST.** Two facts of 25.04, both verified
against the live box (2026-08-12): the deprecated REST bridge does not honour
role-scoped keys, and — the one that shaped this module — **TrueNAS revokes any
user-linked API key presented over plain HTTP**. So the transport is
`wss://{host}/api/current`, always TLS (`_ws_url` refuses to build a ws:// URL:
a plaintext attempt would not merely fail, it would destroy the credential),
with one `auth.login_ex` handshake per session and JSON-RPC method calls —
`system.info`, `pool.query`, `disk.query`, `alert.list` — where REST GETs were.
This is what lets the credential be a **read-only** key (Read-Only Admin role),
matching the Proxmox `PVEAuditor` premise.

**On SSRF (§4c).** `TRUENAS_HOST` is deploy-time configuration read from the
environment, never from a request, so — exactly like `PROXMOX_HOST` and
`DATABASE_URL` — it is not a user-controlled outbound path and the probe's
address validator does not apply. Keeping provider endpoints out of the settings
table (which stores only bool/int/enum/env_ref) is what preserves that.

**On disclosure.** The dashboard panel is public. Disk **serials are omitted at
the dataclass level** (structural, like `Node` omitting `ssl_fingerprint`) so no
consumer can leak them, and `panels.py` coarsens poll-failure reasons to a closed
public string set — the admin settings screen reads the real detail. One rule
specific to this transport: **a JSON-RPC error object is never quoted whole** —
TrueNAS embeds the failing call's params, *including the API key*, in
`error.data.trace` (observed live), so `_rpc_error_text` extracts only the error
code and `errname`.

**Shapes.** The `# SHAPE:` notes below were confirmed live over the WS API
(2026-08-12): `pool.query`/`disk.query`/`alert.list` rows match what the REST
bridge served, because REST was a bridge over these very methods. The one
casualty is `smart.test.results`, which fails server-side on 25.04.2.6 (its own
response schema rejects a field it emits) — best-effort, degrades to unknown.
"""

from __future__ import annotations

import json
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import websocket
from django.conf import settings

from .base import (
    CRITICAL_ALERT_LEVELS,
    ProviderAlert,
    ProviderStatus,
    ProviderUnavailable,
)

logger = logging.getLogger("services.providers.truenas")

#: Alert levels TrueNAS raises, worst last. "Critical/high" for Discord — the
#: levels at ERROR and above — is the shared `CRITICAL_ALERT_LEVELS` imported
#: above (the forwarder in `poll.py` uses the same set), re-exported here so a
#: reader of this module still finds the threshold beside the levels it filters.
ALERT_LEVELS = ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")


# --- Report dataclasses (TrueNAS-specific; the shared ProviderAlert is in base) ---


@dataclass(frozen=True)
class Pool:
    """One ZFS pool: capacity plus health and any running scrub/resilver."""

    name: str
    #: ONLINE / DEGRADED / FAULTED / OFFLINE / UNKNOWN — upper-cased as TrueNAS
    #: reports it.
    status: str
    healthy: bool
    used_bytes: int | None
    total_bytes: int | None
    free_bytes: int | None
    #: SCRUB / RESILVER / "" when nothing is running.
    scan_function: str = ""
    #: FINISHED / SCANNING / CANCELED / "" — the state of the above.
    scan_state: str = ""
    #: 0–100 progress of a running scan, else None.
    scan_percentage: float | None = None

    def as_payload(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "healthy": self.healthy,
            "used_bytes": self.used_bytes,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "scan_function": self.scan_function,
            "scan_state": self.scan_state,
            "scan_percentage": self.scan_percentage,
        }


@dataclass(frozen=True)
class Disk:
    """One physical disk. **No serial field** — see the module docstring."""

    name: str
    model: str
    #: HDD / SSD as TrueNAS classifies it.
    disk_type: str
    size_bytes: int | None
    #: Owning pool name, or "" for an unassigned/spare disk.
    pool: str
    #: Best-effort from `POST /disk/temperatures`; None when unread.
    temperature_c: int | None = None
    #: PASS / FAIL / "" (unknown) from the latest SMART test, best-effort.
    smart_status: str = ""

    def as_payload(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "disk_type": self.disk_type,
            "size_bytes": self.size_bytes,
            "pool": self.pool,
            "temperature_c": self.temperature_c,
            "smart_status": self.smart_status,
        }


@dataclass(frozen=True)
class TrueNASSystem:
    """The box itself: version, uptime, memory and load."""

    version: str
    hostname: str
    uptime_s: int | None
    mem_total_bytes: int | None
    #: Best-effort: `system/info` reports total physmem but not instantaneous
    #: usage, which needs the reporting API — None until that is wired.
    mem_used_bytes: int | None
    #: One-minute load average.
    load_avg: float | None
    cores: int | None

    def as_payload(self) -> dict:
        return {
            "version": self.version,
            "hostname": self.hostname,
            "uptime_s": self.uptime_s,
            "mem_total_bytes": self.mem_total_bytes,
            "mem_used_bytes": self.mem_used_bytes,
            "load_avg": self.load_avg,
            "cores": self.cores,
        }


@dataclass(frozen=True)
class BackupTask:
    """The last-run outcome of a replication or cloud-sync task."""

    #: "replication" or "cloudsync".
    kind: str
    name: str
    #: FINISHED / ERROR / RUNNING / PENDING / "" — upper-cased.
    state: str
    at: datetime | None = None

    def as_payload(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "state": self.state,
            "at": self.at.isoformat() if self.at else None,
        }


@dataclass(frozen=True)
class TrueNASReport:
    """Everything the TrueNAS adapter section renders, from one poll."""

    system: TrueNASSystem | None
    pools: tuple[Pool, ...]
    disks: tuple[Disk, ...]
    backups: tuple[BackupTask, ...]
    alerts: tuple[ProviderAlert, ...]

    def as_payload(self) -> dict:
        return {
            "system": self.system.as_payload() if self.system else None,
            "pools": [pool.as_payload() for pool in self.pools],
            "disks": [disk.as_payload() for disk in self.disks],
            "backups": [task.as_payload() for task in self.backups],
            "alerts": [alert.as_payload() for alert in self.alerts],
        }


def _rpc_error_text(method: str, error) -> str:
    """A safe, short description of a JSON-RPC error.

    Deliberately built from the error **code and `errname` only**. TrueNAS
    embeds the failing call's params — including the API key — in
    `error.data.trace` (observed live, 2026-08-12), so the full error object
    must never reach an exception message or a log line. `errname` is a
    middleware constant (`EINVAL`, `ENOTAUTHENTICATED`, …) and carries nothing
    caller- or server-composed.
    """
    code = error.get("code") if isinstance(error, dict) else None
    errname = ""
    if isinstance(error, dict) and isinstance(error.get("data"), dict):
        errname = error["data"].get("errname") or ""
    suffix = f" ({errname})" if errname else ""
    return f"TrueNAS returned a JSON-RPC error {code} for {method}{suffix}"


def _rows(value, method: str) -> list[dict]:
    """The list-of-objects shape every `*.query` answers with, validated —
    the WS counterpart of the old REST `expect=list` check."""
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ProviderUnavailable(
            f"TrueNAS returned an unexpected response shape for {method}"
        )
    return value


class _Session:
    """One JSON-RPC 2.0 conversation over an open WebSocket.

    Thin by design: `call()` sends one request and returns its `result`,
    skipping any interleaved event notifications (messages without our id —
    the middleware broadcasts collection updates on the same socket). The
    transport is created by `TrueNASProvider._connect`, which the tests
    monkeypatch to substitute a fake — nothing below this seam knows about
    sockets, so the fake stays a pure method-router.
    """

    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    def call(self, method: str, params: list | None = None):
        self._id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params if params is not None else [],
        }
        try:
            self._ws.send(json.dumps(request))
            while True:
                message = json.loads(self._ws.recv())
                if isinstance(message, dict) and message.get("id") == self._id:
                    break
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            # The exception text of a transport error names hosts and timeouts,
            # never the credential (the key travels inside a sent frame, not in
            # a URL or header a stringified exception would echo).
            raise ProviderUnavailable(f"{type(exc).__name__} during {method}") from exc
        if "error" in message:
            raise ProviderUnavailable(_rpc_error_text(method, message["error"]))
        return message.get("result")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing is best-effort by definition
            pass


#: What each non-SUCCESS `auth.login_ex` answer means for the operator — OUR
#: closed strings, not server text. EXPIRED covers revoked keys too, and the
#: by-far most likely cause of an unexpectedly dead key is 25.04's
#: plaintext-transport revocation, hence the hint in its message.
_AUTH_FAILURES = {
    "EXPIRED": (
        "TrueNAS reports the API key as expired or revoked. TrueNAS revokes any "
        "key presented over plain HTTP — create a fresh key and use it only "
        "through this app."
    ),
    "AUTH_ERR": (
        "TrueNAS refused the API key. Check TRUENAS_API_USER and that the key "
        "belongs to that user."
    ),
    "OTP_REQUIRED": (
        "The TrueNAS account requires two-factor auth, which unattended polling "
        "cannot answer — use a service account without 2FA."
    ),
}


class TrueNASProvider:
    """Read-only TrueNAS SCALE, via the WebSocket JSON-RPC API."""

    name = "truenas"

    # --- Configuration -----------------------------------------------------

    def is_configured(self) -> bool:
        """Credentials present. No network — called on request paths."""
        return bool(self._host() and self._key() and self._user())

    def configuration_gaps(self) -> list[str]:
        """Missing REQUIRED env vars — but only when *partly* configured.

        Wholly unset is §7d's normal "not configured" choice and stays quiet;
        one or two of the three set is a mistake worth naming. The realistic
        case is a TN2-era deploy (host + key) upgrading past TN-WS, where the
        new `TRUENAS_API_USER` is the silently-missing third piece — without
        this, the settings row says only "Not configured." while the operator
        stares at a host and key that are plainly set. Env-only, no network,
        safe on request paths; the settings provider row reads it by
        reflection.
        """
        values = {
            "TRUENAS_HOST": self._host(),
            "TRUENAS_API_KEY": self._key(),
            "TRUENAS_API_USER": self._user(),
        }
        if all(values.values()) or not any(values.values()):
            return []
        return [name for name, value in values.items() if not value]

    @staticmethod
    def _host() -> str:
        return (getattr(settings, "TRUENAS_HOST", "") or "").strip().rstrip("/")

    @staticmethod
    def _key() -> str:
        return (getattr(settings, "TRUENAS_API_KEY", "") or "").strip()

    @staticmethod
    def _user() -> str:
        return (getattr(settings, "TRUENAS_API_USER", "") or "").strip()

    def _ws_url(self) -> str:
        """`wss://…/api/current` from `TRUENAS_HOST` — always TLS, never ws://.

        This is a security control, not a convenience: TrueNAS 25.04+ REVOKES a
        user-linked API key the moment it is presented over plain HTTP
        ("insecure transport", verified live 2026-08-12), so a plaintext attempt
        would not merely fail — it would destroy the credential. An `http://`
        host therefore contributes only its hostname (its port belongs to the
        plaintext listener); an `https://` one keeps `host:port` verbatim.

        Scheme and address are still validated separately, so an operator who
        typed exactly `https://` is told the address is missing, not the scheme.
        """
        host = self._host()
        example = "e.g. https://truenas.example.ts.net"
        parts = urlsplit(host)
        if parts.scheme not in ("http", "https"):
            raise ProviderUnavailable(f"TRUENAS_HOST must start with https://, {example}")
        if not parts.netloc:
            raise ProviderUnavailable(
                f"TRUENAS_HOST has a scheme but no address — it is just {host!r}. "
                f"Add the host, {example}"
            )
        netloc = parts.netloc if parts.scheme == "https" else parts.hostname
        return f"wss://{netloc}/api/current"

    def _connect(self) -> _Session:
        """Open and TLS-wrap the WebSocket. Authentication is `_login`'s job.

        Split from `_login` so `status()` can keep the Proxmox distinction
        between "unreachable" (connection failed) and "reachable but refused"
        (login answered, negatively).
        """
        _warn_once_about_unverified_tls()
        if settings.TRUENAS_VERIFY_TLS:
            sslopt = {}
        else:
            sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        try:
            ws = websocket.create_connection(
                self._ws_url(),
                sslopt=sslopt,
                timeout=settings.TRUENAS_TIMEOUT_S,
            )
        except (websocket.WebSocketException, OSError, ValueError) as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return _Session(ws)

    def _login(self, session: _Session) -> str:
        """`auth.login_ex` with the user-linked key; returns the response type.

        25.04 keys are linked to a user, so the mechanism is `API_KEY_PLAIN`
        with BOTH the username and the key. The key crosses once, inside the
        TLS-wrapped frame — never in a URL and never on a redirectable request.
        """
        result = session.call(
            "auth.login_ex",
            [{
                "mechanism": "API_KEY_PLAIN",
                "username": self._user(),
                "api_key": self._key(),
            }],
        )
        if not isinstance(result, dict):
            return "AUTH_ERR"
        return result.get("response_type") or "AUTH_ERR"

    def status(self) -> ProviderStatus:
        """Configured, reachable and permitted, via one authenticated ping.

        `reachable` is the socket; `permitted` is the login **plus one
        `system.info` read** — the read matters because a key whose user holds
        no role logs in successfully and then fails every query, which is this
        provider's version of Proxmox's blind-token trap. A failed login is an
        explicit `response_type`, mapped to our closed `_AUTH_FAILURES` strings.
        """
        if not self.is_configured():
            gaps = self.configuration_gaps()
            detail = (
                f"Partly configured — missing {', '.join(gaps)}."
                if gaps
                else "No TrueNAS credentials set."
            )
            return ProviderStatus(configured=False, detail=detail)
        try:
            session = self._connect()
        except ProviderUnavailable as exc:
            return ProviderStatus(configured=True, reachable=False, detail=str(exc))
        try:
            response = self._login(session)
            if response != "SUCCESS":
                detail = _AUTH_FAILURES.get(
                    response, f"TrueNAS login failed ({response})."
                )
                return ProviderStatus(
                    configured=True, reachable=True, permitted=False, detail=detail
                )
            self._system(session)
        except ProviderUnavailable as exc:
            return ProviderStatus(
                configured=True, reachable=True, permitted=False, detail=str(exc)
            )
        finally:
            session.close()
        return ProviderStatus(configured=True, reachable=True, permitted=True)

    def guests(self, status: ProviderStatus | None = None) -> list:
        """Always `[]`: TrueNAS is storage, not a hypervisor — see module docs.

        An empty list is the honest answer the base contract asks for (never a
        silent failure), so this never raises and `location_for` is never
        reached from the enrichment path.
        """
        return []

    def location_for(self, guest) -> str:
        # Unreachable: guests() is always empty. Present to satisfy the seam.
        return ""

    def infrastructure(self, status: ProviderStatus | None = None) -> None:
        """Always `None`: no Proxmox-style cluster/nodes table (base §7d).

        TrueNAS's storage data rides `report()` instead, which panels.py renders
        as the TrueNAS adapter section rather than the nodes panel.
        """
        return None

    # --- Report (the optional capability) ----------------------------------

    def report(self, status: ProviderStatus | None = None) -> TrueNASReport | None:
        """Pools, disks, system, backups and native alerts — the whole section.

        Mirrors `infrastructure()`'s failure discipline: a genuine read failure
        on a **core** surface (system/pools/disks) raises `ProviderUnavailable`
        so the poll records the reason rather than caching a half-empty panel.
        The **best-effort** surfaces (disk temps, SMART, backups, alerts) degrade
        to empty on their own failure and never take the section down with them —
        a spun-down disk that will not report its temperature must not blank the
        pool capacity beside it.
        """
        status = status or self.status()
        if not status.configured:
            raise ProviderUnavailable("No TrueNAS credentials set.")
        if not status.reachable or not status.permitted:
            raise ProviderUnavailable(status.detail)

        session = self._connect()
        try:
            response = self._login(session)
            if response != "SUCCESS":
                raise ProviderUnavailable(
                    _AUTH_FAILURES.get(response, f"TrueNAS login failed ({response}).")
                )
            system = self._system(session)
            pools, disk_pool_map = self._pools(session)
            disks = self._disks(session, disk_pool_map)
            backups = self._backups(session)
            alerts = self._alerts(session)
        finally:
            session.close()
        return TrueNASReport(
            system=system, pools=pools, disks=disks, backups=backups, alerts=alerts
        )

    def _system(self, session: _Session) -> TrueNASSystem:
        """`system.info` — version, uptime, total memory, load, cores."""
        info = session.call("system.info")
        if not isinstance(info, dict):
            raise ProviderUnavailable(
                "TrueNAS returned an unexpected response shape for system.info"
            )
        loadavg = info.get("loadavg")
        load_1m = loadavg[0] if isinstance(loadavg, list) and loadavg else None
        return TrueNASSystem(
            version=info.get("version") or "",
            hostname=info.get("hostname") or "",
            uptime_s=_as_int(info.get("uptime_seconds")),
            mem_total_bytes=_as_int(info.get("physmem")),
            # Instantaneous usage needs the reporting API; left None for now.
            mem_used_bytes=None,
            load_avg=_as_float(load_1m),
            cores=_as_int(info.get("cores")),
        )

    def _pools(
        self, session: _Session
    ) -> tuple[tuple[Pool, ...], dict[str, str]]:
        """`pool.query` — one row per pool, plus a `{devname: pool}` membership map.

        The membership map is built here, from each pool's `topology`, because
        that is the **only** place the disk↔pool association is actually reported:
        verified against TrueNAS SCALE 25.04, `disk.query` returns `pool: null`
        for every disk (the field exists but is unpopulated), while the pool
        topology carries the vdev tree whose leaves name their `disk` (the
        devname). Same shape as the Proxmox LXC-address finding — the obvious
        field is empty and the truth is on a different endpoint — so `_disks`
        reads this map rather than the disk row's own `pool`.
        """
        rows = _rows(session.call("pool.query") or [], "pool.query")
        pools = []
        disk_pool_map: dict[str, str] = {}
        for row in rows:
            name = row.get("name")
            if not name:
                continue
            for devname in _topology_devnames(row.get("topology")):
                disk_pool_map[devname] = name
            status = (row.get("status") or "").upper()
            healthy = row.get("healthy")
            healthy = bool(healthy) if healthy is not None else status == "ONLINE"
            used, total, free = self._pool_capacity(session, row)
            # SHAPE: `scan` is null when nothing is running, else an object with
            # function/state/percentage.
            scan = row.get("scan") or {}
            pools.append(
                Pool(
                    name=name,
                    status=status,
                    healthy=healthy,
                    used_bytes=used,
                    total_bytes=total,
                    free_bytes=free,
                    scan_function=(scan.get("function") or "").upper(),
                    scan_state=(scan.get("state") or "").upper(),
                    scan_percentage=_as_float(scan.get("percentage")),
                )
            )
        return tuple(pools), disk_pool_map

    def _pool_capacity(
        self, session: _Session, pool_row: dict
    ) -> tuple[int | None, int | None, int | None]:
        """Capacity as `(used, total, free)`, with a version-tolerant fallback.

        Recent SCALE puts `size`/`allocated`/`free` on the pool object (confirmed
        live over WS on 25.04). Older releases do not, so when they are absent
        the pool's **root dataset** carries `used`/`available` (their sum is the
        pool total), read via a `pool.dataset.query` filter — no URL to build, so
        no path-escaping concern. The fallback path is exercised in the fakes.
        """
        size = _as_int(pool_row.get("size"))
        allocated = _as_int(pool_row.get("allocated"))
        free = _as_int(pool_row.get("free"))
        if size is not None and allocated is not None:
            return allocated, size, (free if free is not None else size - allocated)

        name = pool_row.get("name")
        if not name:
            return None, None, None
        try:
            # SHAPE: dataset fields are {"value":"1.2G","rawvalue":"...","parsed":N}.
            found = session.call("pool.dataset.query", [[["id", "=", str(name)]]])
        except ProviderUnavailable as exc:
            logger.warning("could not read dataset capacity for pool %s: %s", name, exc)
            return None, None, None
        data = (
            found[0]
            if isinstance(found, list) and found and isinstance(found[0], dict)
            else {}
        )
        used = _nested_bytes(data.get("used"))
        avail = _nested_bytes(data.get("available"))
        total = used + avail if used is not None and avail is not None else None
        return used, total, avail

    def _disks(
        self, session: _Session, disk_pool_map: dict[str, str] | None = None
    ) -> tuple[Disk, ...]:
        """`disk.query`, enriched best-effort with temperatures, SMART and pool.

        The temperature and SMART sub-reads are best-effort: `disk.temperatures`
        queries drives live and can be slow or spin an idle disk up, and the
        SMART result surface is broken server-side on 25.04.2.6 (see
        `_smart_status`) — neither may fail the disk list.

        `pool` comes from `disk_pool_map` (built by `_pools` from pool topology),
        **not** the disk row's own `pool` field, which TrueNAS 25.04 leaves null —
        see `_pools`. A disk in no pool (a spare, an unassigned drive) maps to "".
        """
        disk_pool_map = disk_pool_map or {}
        rows = _rows(session.call("disk.query") or [], "disk.query")
        names = [row.get("name") for row in rows if row.get("name")]
        temperatures = self._disk_temperatures(session, names)
        smart = self._smart_status(session)
        disks = []
        for row in rows:
            name = row.get("name")
            if not name:
                continue
            disks.append(
                Disk(
                    name=name,
                    model=row.get("model") or "",
                    disk_type=(row.get("type") or "").upper(),
                    size_bytes=_as_int(row.get("size")),
                    pool=disk_pool_map.get(name, ""),
                    temperature_c=temperatures.get(name),
                    smart_status=smart.get(name, ""),
                )
            )
        return tuple(disks)

    def _disk_temperatures(
        self, session: _Session, names: list
    ) -> dict[str, int]:
        """`disk.temperatures(names)` → `{name: celsius}`. Best-effort → `{}`."""
        if not names:
            return {}
        try:
            data = session.call("disk.temperatures", [list(names)])
        except ProviderUnavailable as exc:
            logger.warning("could not read disk temperatures: %s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            name: int(temp)
            for name, temp in data.items()
            if isinstance(temp, (int, float))
        }

    def _smart_status(self, session: _Session) -> dict[str, str]:
        """Latest SMART test result per disk → `{name: PASS|FAIL}`. Best-effort.

        SHAPE: `smart.test.results` returns a list of `{"disk": "sda", "tests":
        [{"status": "SUCCESS"|"FAILED"|"RUNNING"}]}`, newest test last. Anything
        not matching is skipped rather than guessed — the authoritative
        failing-drive signal is pool health plus the native alert feed, so this
        is an enhancement, not load-bearing. **Known casualty:** on 25.04.2.6
        this method fails SERVER-side (the middleware's own response schema
        rejects a `power_on_hours_ago` field it emits — verified live), so on
        that release every disk reads SMART-unknown. This except-and-empty is
        that degradation working as designed, not a gap.
        """
        try:
            rows = _rows(
                session.call("smart.test.results") or [], "smart.test.results"
            )
        except ProviderUnavailable as exc:
            logger.warning("could not read SMART results: %s", exc)
            return {}
        out: dict[str, str] = {}
        for row in rows:
            disk = row.get("disk")
            tests = row.get("tests")
            if not disk or not isinstance(tests, list) or not tests:
                continue
            last = tests[-1]
            if not isinstance(last, dict):
                continue
            raw = (last.get("status") or "").upper()
            if raw in ("SUCCESS", "PASSED", "PASS"):
                out[disk] = "PASS"
            elif raw in ("FAILED", "FAIL"):
                out[disk] = "FAIL"
        return out

    def _backups(self, session: _Session) -> tuple[BackupTask, ...]:
        """Replication + cloud-sync last-run outcomes. Each source best-effort."""
        tasks: list[BackupTask] = []
        tasks.extend(self._replication(session))
        tasks.extend(self._cloudsync(session))
        return tuple(tasks)

    def _replication(self, session: _Session) -> list[BackupTask]:
        try:
            rows = _rows(session.call("replication.query") or [], "replication.query")
        except ProviderUnavailable as exc:
            logger.warning("could not read replication tasks: %s", exc)
            return []
        out = []
        for row in rows:
            # SHAPE: `state` is {"state": "FINISHED"|"ERROR"|..., "datetime": {"$date": ms}}.
            state = row.get("state") if isinstance(row.get("state"), dict) else {}
            out.append(
                BackupTask(
                    kind="replication",
                    name=row.get("name") or "",
                    state=(state.get("state") or "").upper(),
                    at=_parse_ts(state.get("datetime")),
                )
            )
        return out

    def _cloudsync(self, session: _Session) -> list[BackupTask]:
        try:
            rows = _rows(session.call("cloudsync.query") or [], "cloudsync.query")
        except ProviderUnavailable as exc:
            logger.warning("could not read cloud-sync tasks: %s", exc)
            return []
        out = []
        for row in rows:
            # SHAPE: `job` is {"state": "SUCCESS"|"FAILED"|..., "time_finished": {"$date": ms}}
            # while running/idle it may be null.
            job = row.get("job") if isinstance(row.get("job"), dict) else {}
            out.append(
                BackupTask(
                    kind="cloudsync",
                    name=row.get("description") or "",
                    state=(job.get("state") or "").upper(),
                    at=_parse_ts(job.get("time_finished")),
                )
            )
        return out

    def _alerts(self, session: _Session) -> tuple[ProviderAlert, ...]:
        """`alert.list` → the box's own active, undismissed alerts.

        This is the operator's actual want: the system that should have flagged a
        degrading disk. Best-effort — a failure here empties the alert list
        rather than failing the whole report.
        """
        try:
            rows = _rows(session.call("alert.list") or [], "alert.list")
        except ProviderUnavailable as exc:
            logger.warning("could not read TrueNAS alerts: %s", exc)
            return ()
        alerts = []
        for row in rows:
            if row.get("dismissed"):
                continue
            # SHAPE: {"uuid": "...", "level": "CRITICAL", "formatted": "...",
            #         "dismissed": false, "datetime": {"$date": ms}, "klass": "..."}.
            key = str(row.get("uuid") or row.get("id") or "")
            if not key:
                continue
            alerts.append(
                ProviderAlert(
                    key=key,
                    level=(row.get("level") or "").upper(),
                    message=row.get("formatted") or row.get("text") or "",
                    at=_parse_ts(row.get("datetime")),
                    # `klass` is TrueNAS's alert-type identifier (a provider enum,
                    # not the instance text) — the safe, coarsened category the
                    # public panel shows in place of the untrusted `formatted`
                    # message. See `ProviderAlert` in base.py.
                    category=str(row.get("klass") or ""),
                )
            )
        return tuple(alerts)


#: Process-local latch so the TLS warning is said once, not once per request.
_tls_warning_said = False


def _warn_once_about_unverified_tls() -> None:
    """Say plainly when the API key crosses an unverified TLS connection.

    Same trade-off and rationale as the Proxmox client: with verification off,
    an *active* man in the middle can still terminate our wss:// connection and
    capture the key during the login handshake. Default is off because a homelab
    TrueNAS commonly presents a self-signed or private-CA certificate and
    defaulting to True would fail closed for almost everyone — but it must never
    be *silent*. Two mitigations bound the exposure: the transport is always TLS
    (`_ws_url` never builds ws:// — a passive listener sees nothing), and the
    credential is a **read-only** key again now that the WS API honours
    role-scoped keys (TN-WS restored the Proxmox-`PVEAuditor`-class premise the
    REST bridge broke).
    """
    global _tls_warning_said
    if _tls_warning_said or settings.TRUENAS_VERIFY_TLS:
        return
    _tls_warning_said = True
    logger.warning(
        "TRUENAS_VERIFY_TLS is off: the TrueNAS connection is TLS (wss://) but "
        "the certificate is not verified, so an active man-in-the-middle could "
        "capture the API key during login. The key is read-only, which bounds "
        "the damage. Set TRUENAS_VERIFY_TLS=True once TrueNAS presents a "
        "certificate your trust store accepts."
    )


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _nested_bytes(field) -> int | None:
    """A TrueNAS dataset size field `{"parsed": N, "rawvalue": "N"}` → `N`."""
    if isinstance(field, dict):
        return _as_int(field.get("parsed")) or _as_int(field.get("rawvalue"))
    return _as_int(field)


def _topology_devnames(topology) -> list[str]:
    """Every leaf disk's devname under a pool's `topology`. Tolerant by design.

    The topology is a tree: each group (`data`/`cache`/`log`/`spare`/`special`/
    `dedup`) holds vdevs, a vdev is either a leaf (`type: DISK`, carrying a `disk`
    devname) or a container with `children`. A RAIDZ vdev is a container whose
    children are the leaves. Walk both, collect every `disk`. A malformed or
    partial topology yields whatever devnames it can and never raises — a shape
    surprise here must cost the disk↔pool labels, not the whole report.
    """
    names: list[str] = []

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        dev = node.get("disk")
        if dev:
            names.append(dev)
        for child in node.get("children") or []:
            walk(child)

    if isinstance(topology, dict):
        for group in topology.values():
            for vdev in group or []:
                walk(vdev)
    return names


def _parse_ts(raw) -> datetime | None:
    """A TrueNAS timestamp — `{"$date": ms}` or a bare epoch — to a UTC datetime."""
    if isinstance(raw, dict):
        raw = raw.get("$date")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
